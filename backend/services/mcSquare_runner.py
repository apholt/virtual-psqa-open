"""
MCsquare Monte Carlo runner — openMCsquare integration for Virtual PSQA.

The real dose calculation is delegated to a standalone subprocess worker
(mcSquare_worker.py) that drives the validated `Process/` package on the
planning CT. Running it out-of-process:

  * uses the SDC's validated CT-relative isocenter transform + DeliveredProtons
    scaling (fixes the ~3 cm depth offset the old synthetic water phantom had),
  * isolates the package's CWD-relative behaviour and global state from the
    threaded FastAPI server,
  * streams progress and supports cancellation during the (minutes-long) sim.

The worker writes CT-aligned, Gy-scaled DoseGrid bundles directly into the
output directory:  mc_dose.npz (summed) + mc_dose_beam{N}.npz (per beam),
preserving the output contract that gamma_analysis and the dose viewer rely on.

Public interface (unchanged, called by services.job_runner):
    build_mcSquare_input(plan_id, db) -> str      # input dir (prep only)
    run_mcSquare(input_dir, output_dir, job_id, db) -> str   # summed .npz path

A mock path is retained for MCSQUARE_SIMULATION_MODE=True (no binary needed).
"""
from __future__ import annotations

import logging
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np
from sqlalchemy.orm import Session

from config import settings
from models.plan import Plan
from services.dose_grid import DoseGrid
from services.job_control import (
    JobCancelled,
    is_cancelled,
    register_process,
    unregister_process,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths / progress helpers
# ---------------------------------------------------------------------------

def _plan_output_dir(plan_id: int) -> Path:
    return Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "mcSquare_output"


def _plan_input_dir(plan_id: int) -> Path:
    return Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "mcSquare_input"


def _update_progress(db: Session, job_id: int, progress: float) -> None:
    from models.qa_job import QAJob

    job = db.query(QAJob).filter_by(id=job_id).first()
    if job:
        job.progress = max(0.0, min(1.0, progress))
        db.commit()


def _find_first(dicom_store_path: str, sop_class_uids: set[str]) -> Optional[str]:
    """Return the first .dcm in the store whose SOPClassUID is in the set."""
    import pydicom

    for path in Path(dicom_store_path).rglob("*.dcm"):
        try:
            dcm = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        except Exception:
            continue
        if str(getattr(dcm, "SOPClassUID", "")) in sop_class_uids:
            return str(path)
    return None


_UID_RTPLAN = {"1.2.840.10008.5.1.4.1.1.481.8", "1.2.840.10008.5.1.4.1.1.481.5"}
_UID_RTDOSE = {"1.2.840.10008.5.1.4.1.1.481.2"}
_UID_CT = {"1.2.840.10008.5.1.4.1.1.2"}


# ---------------------------------------------------------------------------
# build_mcSquare_input — validation / prep only (worker builds its own input)
# ---------------------------------------------------------------------------

def build_mcSquare_input(plan_id: int, db: Session) -> str:
    """
    Prepare/validate inputs for the MCsquare job and return the input dir.

    The worker constructs its own MCsquare input (CT.mhd / PlanPencil.txt /
    config.txt) from the plan's DICOM store, so this no longer hand-builds a
    phantom. It just ensures the store has what the real path needs and returns
    a stable input-dir path (kept for signature compatibility with job_runner).
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found")
    store = plan.dicom_store_path
    if not store or not Path(store).is_dir():
        raise FileNotFoundError(f"Plan {plan_id} has no DICOM store at {store!r}")

    input_dir = _plan_input_dir(plan_id)
    input_dir.mkdir(parents=True, exist_ok=True)

    # Soft validation — log clearly, but only hard-fail when running the real
    # path (mock builds a dose from the TPS RTDose and needs only that).
    if not _should_mock():
        if _find_first(store, _UID_RTPLAN) is None:
            raise FileNotFoundError(f"No RT(Ion)Plan found in {store}")
        if _find_first(store, _UID_CT) is None:
            raise FileNotFoundError(
                f"No CT series found in {store}. The MCsquare secondary calc "
                f"needs the planning CT — export the CT image set (and RTStruct) "
                f"to the watch folder alongside the plan, not just plan+RTDose."
            )
    if _find_first(store, _UID_RTDOSE) is None:
        logger.warning(
            f"Plan {plan_id}: no RTDose in store — gamma vs TPS will have no "
            f"reference until one is present."
        )
    return str(input_dir)


# ---------------------------------------------------------------------------
# Mock path (MCSQUARE_SIMULATION_MODE=True)
# ---------------------------------------------------------------------------

def _should_mock() -> bool:
    if settings.MCSQUARE_SIMULATION_MODE:
        return True
    # If somehow enabled without an install, fall back to mock rather than crash.
    backend_dir = Path(__file__).resolve().parent.parent
    candidates = [
        Path(settings.MCSQUARE_HOME),
        backend_dir.parent / "MCsquare",
        backend_dir.parent / "mcSquare",
    ]
    for c in candidates:
        if (c / "BDL").is_dir():
            return False
    return True


def _mock_simulate(
    plan_id: int, output_dir: str, job_id: int, db: Session, force: bool = False
) -> str:
    """Build a mock MC dose by perturbing the TPS RTDose. Dev-only."""
    import time
    from dicom.rtdose_parser import find_rtdose_file, find_beam_rtdose_files
    from services.gamma_analysis import load_rtdose

    plan = db.query(Plan).filter_by(id=plan_id).first()
    rtdose_path = (
        find_rtdose_file(plan.dicom_store_path, plan_uid=plan.rtplan_uid)
        if plan
        else None
    )
    if not rtdose_path:
        raise FileNotFoundError(
            f"No RTDose found for plan {plan_id} — cannot build mock MC dose."
        )
    tps = load_rtdose(rtdose_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if force:
        for f in out_dir.glob("mc_dose_beam*.npz"):
            try:
                f.unlink()
            except Exception:
                pass
        summed_file = out_dir / "mc_dose.npz"
        if summed_file.exists():
            try:
                summed_file.unlink()
            except Exception:
                pass

    beam_rtdoses = (
        find_beam_rtdose_files(plan.dicom_store_path, plan_uid=plan.rtplan_uid)
        if plan
        else {}
    )
    beam_numbers = sorted(beam_rtdoses.keys()) if beam_rtdoses else [1]
    n_beams = len(beam_numbers)

    rng = np.random.default_rng(plan_id)
    summed_arr = None

    for i, beam_no in enumerate(beam_numbers):
        beam_path = out_dir / f"mc_dose_beam{beam_no}.npz"
        if not force and beam_path.is_file():
            try:
                bg = DoseGrid.load(str(beam_path))
                summed_arr = bg.array.copy() if summed_arr is None else summed_arr + bg.array
                continue
            except Exception:
                pass

        if is_cancelled(job_id):
            raise JobCancelled()

        # Step simulation progress
        steps = 4
        for s in range(steps):
            if is_cancelled(job_id):
                raise JobCancelled()
            frac = (i + (s + 1) / steps) / n_beams * 0.95
            _update_progress(db, job_id, frac)
            time.sleep(0.05)

        if beam_no in beam_rtdoses:
            b_tps = load_rtdose(beam_rtdoses[beam_no])
            b_noise = rng.normal(1.0, settings.MCSQUARE_MOCK_NOISE, size=b_tps.array.shape).astype(np.float32)
            b_arr = (b_tps.array * b_noise).astype(np.float32)
            mc_beam = DoseGrid(array=b_arr, spacing=b_tps.spacing, origin=b_tps.origin)
        else:
            noise = rng.normal(1.0, settings.MCSQUARE_MOCK_NOISE, size=tps.array.shape).astype(np.float32)
            b_arr = (tps.array * noise / n_beams).astype(np.float32)
            mc_beam = DoseGrid(array=b_arr, spacing=tps.spacing, origin=tps.origin)

        mc_beam.save(beam_path)
        summed_arr = b_arr.copy() if summed_arr is None else summed_arr + b_arr

        # Update composite dose on disk
        comp = DoseGrid(array=summed_arr, spacing=tps.spacing, origin=tps.origin)
        comp.save(out_dir / "mc_dose.npz")

    out_path = out_dir / "mc_dose.npz"
    if summed_arr is not None:
        comp = DoseGrid(array=summed_arr, spacing=tps.spacing, origin=tps.origin)
        comp.save(out_path)
    _update_progress(db, job_id, 1.0)
    return str(out_path)


# ---------------------------------------------------------------------------
# Real path — delegate to mcSquare_worker.py
# ---------------------------------------------------------------------------

def run_mcSquare(
    input_dir: str, output_dir: str, job_id: int, db: Session, force: bool = False
) -> str:
    """
    Run MCsquare (or the mock), updating QAJob.progress. Returns the path to the
    saved summed MC DoseGrid .npz.
    """
    from models.qa_job import QAJob

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    job = db.query(QAJob).filter_by(id=job_id).first()
    plan_id = job.plan_id if job else None
    if plan_id is None:
        raise ValueError(f"Job {job_id} has no associated plan")

    if _should_mock():
        return _mock_simulate(plan_id, output_dir, job_id, db, force=force)

    return _run_worker(plan_id, output_dir, job_id, db, force=force)


def _run_worker(
    plan_id: int,
    output_dir: str,
    job_id: int,
    db: Session,
    ct_dir: Optional[str] = None,
    work_dir: Optional[Path] = None,
    dose_prefix: str = "mc_dose",
    force: bool = False,
) -> str:
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None or not plan.dicom_store_path:
        raise ValueError(f"Plan {plan_id} has no DICOM store path")
    store = Path(plan.dicom_store_path).resolve()

    backend_dir = Path(__file__).resolve().parent.parent
    worker = backend_dir / "mcSquare_worker.py"
    if not worker.exists():
        raise FileNotFoundError(f"MCsquare worker script not found: {worker}")

    candidates = [
        Path(settings.MCSQUARE_HOME).resolve(),
        backend_dir.parent / "MCsquare",
        backend_dir.parent / "mcSquare",
    ]
    install_dir = None
    for c in candidates:
        if (c / "BDL").is_dir():
            install_dir = c
            break

    if install_dir is None:
        raise FileNotFoundError(
            f"openMCsquare install (with BDL/) not found at {candidates}"
        )

    if work_dir is None:
        work_dir = (Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "mcSquare_work").resolve()
    out_dir = Path(output_dir).resolve()

    import platform
    is_win = platform.system().lower() == "windows"

    # Optional overrides — worker defaults: bdl=auto, default scanner
    bdl = getattr(settings, "MCSQUARE_BDL_NAME", "auto")
    scanner = getattr(settings, "MCSQUARE_SCANNER", "default")
    default_exe = "MCsquare_win_avx2.exe" if is_win else "MCsquare_linux"
    exe = getattr(settings, "MCSQUARE_EXE", default_exe) or default_exe
    if is_win and not str(exe).lower().endswith(".exe"):
        exe = default_exe
    elif not is_win and str(exe).lower().endswith(".exe"):
        exe = default_exe
    primaries = settings.MCSQUARE_PRIMARIES
    uncertainty = settings.MCSQUARE_STAT_UNCERTAINTY
    rbe = getattr(settings, "PROTON_RBE", 1.10)

    cmd = [
        sys.executable, str(worker),
        "--dicom-store", str(store),
        "--output-dir", str(out_dir),
        "--work-dir", str(work_dir),
        "--install-dir", str(install_dir),
        "--process-parent", str(backend_dir),
        "--bdl", str(bdl),
        "--scanner", str(scanner),
        "--exe", str(exe),
        "--primaries", str(primaries),
        "--uncertainty", str(uncertainty),
        "--rbe", str(rbe),
        "--dose-prefix", str(dose_prefix),
    ]
    if force:
        cmd.append("--force")
    if plan.rtplan_uid:
        cmd.extend(["--plan-uid", str(plan.rtplan_uid)])
    if ct_dir:
        ct_dir_abs = Path(ct_dir)
        if not ct_dir_abs.is_absolute():
            ct_dir_abs = (backend_dir / ct_dir).resolve()
        cmd.extend(["--ct-dir", str(ct_dir_abs)])

    logger.info(
        f"Launching MCsquare worker for plan {plan_id} (prefix={dose_prefix}, force={force}): store={store} "
        f"{f'ct_dir={ct_dir} ' if ct_dir else ''}bdl={bdl} exe={exe} unc={uncertainty}% rbe={rbe}"
    )

    popen_kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "bufsize": 1,
        "cwd": str(backend_dir),
    }
    if not is_win:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    assert proc.stdout is not None
    register_process(job_id, proc)

    tail: deque[str] = deque(maxlen=60)
    summed_path: Optional[str] = None
    error_msg: Optional[str] = None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            if is_cancelled(job_id):
                proc.terminate()
                raise JobCancelled()
            if line.startswith("PROGRESS "):
                try:
                    _update_progress(db, job_id, min(float(line.split()[1]), 0.99))
                except (ValueError, IndexError):
                    pass
            elif line.startswith("RESULT_SUMMED "):
                summed_path = line.split(" ", 1)[1].strip()
            elif line.startswith("ERROR "):
                error_msg = line.split(" ", 1)[1].strip()
            elif line.startswith("LOG "):
                logger.info(f"[mcSquare worker] {line[4:]}")
    finally:
        unregister_process(job_id)
        if proc.poll() is None:
            proc.terminate()
    ret = proc.wait()
    if is_cancelled(job_id):
        raise JobCancelled()

    if ret != 0 or summed_path is None:
        excerpt = "\n".join(tail)
        detail = f": {error_msg}" if error_msg else ""
        logger.error(f"MCsquare worker failed (exit {ret}){detail}\n{excerpt}")
        raise RuntimeError(
            f"MCsquare worker exited with code {ret}{detail}. Last output:\n{excerpt}"
        )

    _update_progress(db, job_id, 1.0)
    return summed_path


def _mock_simulate_synthetic(
    plan_id: int, fraction_number: int, output_dir: str, job_id: int, db: Session
) -> str:
    """Build a mock synthetic CT MC dose with realistic setup/anatomy perturbation."""
    from dicom.rtdose_parser import find_rtdose_file
    from services.gamma_analysis import load_rtdose

    plan = db.query(Plan).filter_by(id=plan_id).first()
    rtdose_path = find_rtdose_file(plan.dicom_store_path, plan_uid=plan.rtplan_uid) if plan else None
    if not rtdose_path:
        raise FileNotFoundError(
            f"No RTDose found for plan {plan_id} — cannot build mock synthetic CT MC dose."
        )
    tps = load_rtdose(rtdose_path)

    steps = 10
    for i in range(steps):
        if is_cancelled(job_id):
            raise JobCancelled()
        _update_progress(db, job_id, (i + 1) / steps * 0.9)

    # Slight perturbation simulating fraction setup offset (e.g. 1-2% deviation)
    rng = np.random.default_rng(plan_id * 100 + fraction_number)
    noise = rng.normal(0.99, settings.MCSQUARE_MOCK_NOISE * 0.8, size=tps.array.shape).astype(np.float32)
    mc = DoseGrid(
        array=(tps.array * noise).astype(np.float32),
        spacing=tps.spacing,
        origin=tps.origin,
    )
    out_path = Path(output_dir) / "mc_dose_sct.npz"
    saved = mc.save(out_path)
    _update_progress(db, job_id, 1.0)
    return saved


def run_mcSquare_synthetic_ct(
    plan_id: int,
    fraction_number: int,
    ct_dir: str,
    output_dir: str,
    job_id: int,
    db: Session,
) -> str:
    """Run MCsquare on the fraction's synthetic CT scan."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if _should_mock():
        return _mock_simulate_synthetic(plan_id, fraction_number, output_dir, job_id, db)

    work_dir = (
        Path(settings.RESULTS_PATH)
        / f"plan_{plan_id}"
        / "synthetic_ct"
        / f"fx_{fraction_number}"
        / "work"
    ).resolve()
    return _run_worker(
        plan_id,
        output_dir,
        job_id,
        db,
        ct_dir=ct_dir,
        work_dir=work_dir,
        dose_prefix="mc_dose_sct",
    )