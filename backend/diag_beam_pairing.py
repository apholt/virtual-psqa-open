"""
diag_beam_pairing.py — run from backend\ directory.

Verifies the per-beam pairing chain used by gamma_analysis:
  RTPLAN BeamNumber/BeamName  <->  TPS beam RTDose keys  <->  mc_dose_beam{N}.npz

Also dumps recent gamma QA jobs and GammaResult rows so we can see the
zero-rows state directly.

Usage:
  ..\python\python.exe diag_beam_pairing.py [plan_id]     (default plan_id=3)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pydicom

from database import SessionLocal
from config import settings
from models.plan import Plan
from models.qa_job import QAJob
from models.gamma_result import GammaResult
from dicom.rtdose_parser import find_beam_rtdose_files, load_rtdose
from services.dose_grid import DoseGrid

PLAN_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def com_mm(grid) -> tuple | None:
    """Center of mass (patient mm, z/y/x) of the >=50%-max dose region."""
    a = np.asarray(grid.array, dtype=np.float64)
    mx = a.max()
    if mx <= 0:
        return None
    m = a >= 0.5 * mx
    idx = np.argwhere(m)
    w = a[m]
    c = (idx * w[:, None]).sum(0) / w.sum()
    sz, sy, sx = (float(v) for v in grid.spacing)
    oz, oy, ox = (float(v) for v in grid.origin)
    return (oz + c[0] * sz, oy + c[1] * sy, ox + c[2] * sx)


def fmt_com(c) -> str:
    return "None" if c is None else f"({c[0]:8.1f}, {c[1]:8.1f}, {c[2]:8.1f}) mm"


def main() -> None:
    db = SessionLocal()
    try:
        plan = db.query(Plan).filter_by(id=PLAN_ID).first()
        if plan is None:
            print(f"Plan {PLAN_ID} not found"); return
        store = plan.dicom_store_path
        print(f"=== Plan {PLAN_ID}  qa_status={plan.qa_status}")
        print(f"    store: {store}\n")

        # ---- 1. RTPLAN beams: authoritative BeamNumber -> name/gantry -------
        print("--- RTPLAN IonBeamSequence (ordinal vs DICOM BeamNumber) ---")
        plan_beams = []  # (ordinal, beam_number, name, gantry)
        for path in Path(store).glob("*.dcm"):
            try:
                dcm = pydicom.dcmread(str(path), stop_before_pixels=True)
            except Exception:
                continue
            if str(dcm.get("Modality", "")).upper() not in ("RTPLAN", "RTIBTR"):
                continue
            for i, beam in enumerate(getattr(dcm, "IonBeamSequence", []), start=1):
                num = int(getattr(beam, "BeamNumber", 0))
                name = str(getattr(beam, "BeamName", f"Beam{num}"))
                gantry = None
                try:
                    cp0 = beam.IonControlPointSequence[0]
                    gantry = float(getattr(cp0, "GantryAngle", "nan"))
                except Exception:
                    pass
                plan_beams.append((i, num, name, gantry))
                print(f"  ordinal {i}:  BeamNumber={num:<3d} name='{name}'  gantry={gantry}")
        if not plan_beams:
            print("  (no RTPLAN found in store!)")
        print()

        # ---- 2. TPS per-beam RTDoses: how are they keyed? --------------------
        print("--- find_beam_rtdose_files() keys + DICOM ReferencedBeamNumber ---")
        try:
            beam_rtdoses = find_beam_rtdose_files(store) or {}
        except Exception as exc:
            print(f"  find_beam_rtdose_files raised: {exc}")
            beam_rtdoses = {}
        tps_coms = {}
        for key, path in sorted(beam_rtdoses.items()):
            refnum = "?"
            try:
                d = pydicom.dcmread(str(path), stop_before_pixels=True)
                refnum = (d.ReferencedRTPlanSequence[0]
                           .ReferencedFractionGroupSequence[0]
                           .ReferencedBeamSequence[0]
                           .ReferencedBeamNumber)
            except Exception:
                pass
            try:
                g = load_rtdose(path)
                tps_coms[key] = com_mm(g)
            except Exception as exc:
                tps_coms[key] = None
                print(f"  key {key}: load failed: {exc}")
                continue
            print(f"  key {key}:  ReferencedBeamNumber={refnum}  "
                  f"COM={fmt_com(tps_coms[key])}  file={Path(path).name}")
        if not beam_rtdoses:
            print("  (none found)")
        print()

        # ---- 3. MC per-beam npz --------------------------------------------
        print("--- mc_dose_beam{N}.npz ---")
        mc_dir = Path(settings.RESULTS_PATH) / f"plan_{PLAN_ID}" / "mcSquare_output"
        mc_coms = {}
        if mc_dir.exists():
            for npz in sorted(mc_dir.glob("mc_dose_beam*.npz")):
                try:
                    n = int(npz.stem.replace("mc_dose_beam", ""))
                    g = DoseGrid.load(str(npz))
                    mc_coms[n] = com_mm(g)
                    print(f"  beam {n}:  COM={fmt_com(mc_coms[n])}  file={npz.name}")
                except Exception as exc:
                    print(f"  {npz.name}: load failed: {exc}")
        else:
            print(f"  (no dir: {mc_dir})")
        print()

        # ---- 4. COM cross-match matrix (detects swapped pairing) ------------
        if tps_coms and mc_coms:
            print("--- COM distance matrix  MC (rows) vs TPS (cols), mm ---")
            tkeys = sorted(k for k in tps_coms if tps_coms[k] is not None)
            header = "        " + "".join(f"  tps_{k:<6}" for k in tkeys)
            print(header)
            for mk in sorted(mc_coms):
                if mc_coms[mk] is None:
                    continue
                row = f"  mc_{mk:<4}"
                for tk in tkeys:
                    d = float(np.linalg.norm(
                        np.array(mc_coms[mk]) - np.array(tps_coms[tk])))
                    row += f"  {d:8.1f}"
                print(row)
            print("  -> each MC row should be smallest on its SAME-number TPS column.")
            print("     If mc_1 matches tps_2 better (and vice versa), pairing/labels")
            print("     are inverted upstream of gamma_analysis.")
        print()

        # ---- 5. Gamma job + row state ---------------------------------------
        print("--- Recent gamma QAJobs for this plan ---")
        jobs = (db.query(QAJob)
                  .filter_by(plan_id=PLAN_ID, job_type="gamma")
                  .order_by(QAJob.id.desc()).limit(5).all())
        for j in jobs:
            print(f"  job {j.id}: status={j.status} progress={j.progress} "
                  f"started={j.started_at} completed={j.completed_at} "
                  f"err={j.error_message!r}")
        if not jobs:
            print("  (none)")
        print()

        print("--- GammaResult rows for this plan ---")
        rows = (db.query(GammaResult)
                  .filter_by(plan_id=PLAN_ID)
                  .order_by(GammaResult.id.desc()).all())
        for r in rows:
            print(f"  id={r.id} {r.comparison_type:<18} field='{r.field_name}' "
                  f"rate={r.passing_rate} thr={r.threshold} passed={r.passed} "
                  f"fx={r.fraction_number}")
        if not rows:
            print("  (ZERO rows)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
