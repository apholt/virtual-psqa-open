"""
mcSquare_worker.py — standalone MCsquare dose-calculation worker for Virtual PSQA.

Runs in the embedded Python as a SUBPROCESS launched by mcSquare_runner.py.
It drives the *validated* SDC `Process/` pipeline on the real planning CT — the
path that fixes the ~32 mm depth offset the hand-rolled water-phantom code had —
and writes summed + per-beam dose as VPSQA DoseGrid .npz bundles.

Pipeline (mirrors Proton_Shooter.ProtonShooter / MCsquare.MCsquare_simulation,
but drives the .exe directly so we get progress + cancellation):

  1. (optional) copy the DICOM store and apply Countour_Overide water overrides
  2. PatientList().import_patient_data()  -> real CT + RT Ion Plan (+ RTStruct)
  3. MCsquare prep: init dir, export CT.mhd (flipped), import BDL,
     export PlanPencil.txt (real-CT isocenter transform WITH the Y-flip),
     generate + export config.txt (Export_Beam_dose = True)
  4. run MCsquare_win.exe  (cwd = WorkDir, MCsquare_Materials_Dir env set)
  5. import_MCsquare_dose (summed + Dose_Beam{N}.mhd), each already scaled to Gy,
     wrap via RTdose.Initialize_from_MHD (CT-grid alignment), transpose to
     DoseGrid (z,y,x) and save .npz

stdout protocol (one token per line, parsed by the runner):
  LOG <text>            human-readable log
  PROGRESS <0..1>       fractional progress
  RESULT_SUMMED <path>  summed dose npz written
  RESULT_BEAM <n> <path> per-beam dose npz written
  DONE
  ERROR <text>          fatal error (worker exits non-zero)

Everything is stdout so the runner can stream it and the user can paste it.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


def emit(tag, *parts):
    print(tag + (" " + " ".join(str(p) for p in parts) if parts else ""), flush=True)


def log(msg):
    emit("LOG", msg)


def progress(frac):
    emit("PROGRESS", f"{max(0.0, min(1.0, float(frac))):.4f}")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dicom-store", required=True, help="folder with CT + RT Ion Plan (+ RTStruct)")
    p.add_argument("--output-dir", required=True, help="where DoseGrid .npz files are written")
    p.add_argument("--work-dir", required=True, help="MCsquare scratch dir (per-plan)")
    p.add_argument("--install-dir", required=True, help="the mcSquare folder (BDL/Scanners/Materials/exe)")
    p.add_argument("--process-parent", required=True, help="dir that contains the Process package (backend)")
    p.add_argument("--bdl", default="auto",
                   help="BDL name, or 'auto' to pick by machine (gantry vs fixed)")
    p.add_argument("--bdl-gantry", default="GantryTrial5",
                   help="BDL used for gantry machines when --bdl auto")
    p.add_argument("--bdl-fixed", default="TR3FixedBeam",
                   help="BDL used for fixed-beam machines when --bdl auto")
    p.add_argument("--scanner", default="default", help="scanner calibration folder name")
    p.add_argument("--primaries", type=int, default=1_000_000)
    p.add_argument("--uncertainty", type=float, default=1.5, help="target stat uncertainty %%")
    p.add_argument("--dose-scaling", type=float, default=0.9)
    p.add_argument("--rbe", type=float, default=1.1,
                   help="RBE weighting applied to physical dose so output matches "
                        "the RBE-weighted (EFFECTIVE) TPS dose. Use 1.0 to keep physical.")
    p.add_argument("--exe", default="MCsquare_win_avx2.exe", help="MCsquare executable name in install-dir")
    p.add_argument("--ct-dir", default=None, help="override folder containing synthetic CT DICOM series")
    p.add_argument("--dose-prefix", default="mc_dose", help="base filename for output dose npz files")

    p.add_argument("--force", action="store_true",
                   help="force re-simulation of all beams, ignoring existing cached beam doses")
    p.add_argument("--enable-override", action="store_true", help="apply RTStruct water overrides (legacy)")
    p.add_argument("--no-density-override", action="store_true",
                   help="skip applying RTSTRUCT REL_ELEC_DENSITY overrides (couch etc.) to the CT")
    p.add_argument("--plan-uid", default=None, help="SOPInstanceUID of specific RT Ion Plan to simulate")
    return p.parse_args()


def main():
    args = parse_args()

    install_dir = Path(args.install_dir).resolve()
    process_parent = Path(args.process_parent).resolve()
    output_dir = Path(args.output_dir).resolve()
    work_dir = Path(args.work_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # MCsquare.init_simulation_directory() does a bare os.mkdir(WorkDir), which
    # only creates the final segment — so make sure the parents exist first.
    work_dir.mkdir(parents=True, exist_ok=True)

    # The SDC objects resolve `./MCsquare` relative to CWD, so chdir to the
    # install-dir's parent. Safe here: this is a dedicated subprocess.
    os.chdir(str(install_dir.parent))
    sys.path.insert(0, str(process_parent))

    log(f"cwd={os.getcwd()}")
    log(f"install_dir={install_dir}")

    # Normalize CRLF line terminators in BDL / Scanners / Materials files on Linux/Unix
    if platform.system().lower() != "windows":
        try:
            for pattern in ["*.txt", "*.dat"]:
                for cfg_file in install_dir.rglob(pattern):
                    if cfg_file.is_file():
                        try:
                            content = cfg_file.read_bytes()
                            if b"\r\n" in content:
                                cfg_file.write_bytes(content.replace(b"\r\n", b"\n"))
                        except Exception:
                            pass
        except Exception:
            pass

    # numpy + Process imports happen AFTER sys.path / chdir are set
    import numpy as np
    from Process.PatientData import PatientList
    from Process.MCsquare import MCsquare
    from Process.MCsquare_plan import export_plan_for_MCsquare
    from Process.MCsquare_config import generate_MCsquare_config, export_MCsquare_config
    from Process.RTdose import RTdose

    progress(0.02)

    # ------------------------------------------------------------------ load
    load_dir = Path(args.dicom_store).resolve()

    if args.enable_override:
        # Overrides EDIT CT pixel data on disk, so work on a copy, never originals.
        copy_dir = work_dir / "dicom_copy"
        if copy_dir.exists():
            shutil.rmtree(copy_dir)
        shutil.copytree(load_dir, copy_dir)
        load_dir = copy_dir
        log(f"copied DICOM store -> {load_dir}")
        _apply_overrides(load_dir, process_parent)

    if args.ct_dir:
        ct_dir = Path(args.ct_dir).resolve()
        log(f"loading synthetic CT from {ct_dir}")
        ct_patients = PatientList()
        ct_patients.list_dicom_files(str(ct_dir), 1)
        if not ct_patients.list or not ct_patients.list[0].CTimages:
            emit("ERROR", f"no CT series found in synthetic CT dir: {ct_dir}")
            sys.exit(2)

        log(f"loading RT Ion Plan from {load_dir}")
        plan_patients = PatientList()
        plan_patients.list_dicom_files(str(load_dir), 1)
        if not plan_patients.list or not plan_patients.list[0].Plans:
            emit("ERROR", f"no RT Ion Plan found in store: {load_dir}")
            sys.exit(2)

        patient = ct_patients.list[0]
        patient.Plans = plan_patients.list[0].Plans
        if not patient.RTstructs and plan_patients.list[0].RTstructs:
            patient.RTstructs = plan_patients.list[0].RTstructs
    else:
        log(f"loading patient data from {load_dir}")
        patients = PatientList()
        patients.list_dicom_files(str(load_dir), 1)
        if not patients.list:
            emit("ERROR", "no patient found in DICOM store")
            sys.exit(2)
        patient = patients.list[0]

    n_ct = len(patient.CTimages)
    n_plan = len(patient.Plans)
    n_struct = len(patient.RTstructs)
    log(f"found CT_series={n_ct} plans={n_plan} rtstructs={n_struct}")
    if n_ct == 0:
        emit("ERROR", "no CT series in store — the planning CT must be exported with the plan")
        sys.exit(2)
    if n_plan == 0:
        emit("ERROR", "no RT Ion Plan in store")
        sys.exit(2)
    if n_ct > 1:
        log(f"WARNING: {n_ct} CT series present — using the first")
    Plan = None
    if getattr(args, "plan_uid", None):
        import pydicom
        for p in patient.Plans:
            try:
                p_file = getattr(p, "DcmFile", None)
                if p_file:
                    ds = pydicom.dcmread(str(p_file), stop_before_pixels=True, force=True)
                    if str(getattr(ds, "SOPInstanceUID", "")) == args.plan_uid:
                        Plan = p
                        log(f"Matched plan UID {args.plan_uid}: {p_file}")
                        break
            except Exception:
                pass
    if Plan is None:
        if n_plan > 1:
            log(f"WARNING: {n_plan} plans present — using the first")
        Plan = patient.Plans[0]

    # Retain only the chosen plan so only that plan is loaded & simulated
    patient.Plans = [Plan]

    # We only need CT (+ struct for density overrides); skip TPS dose loading.
    patient.RTdoses = []
    # RTStructs are needed for the density override (couch etc.) — keep them
    # unless density override is explicitly disabled AND water override is off.
    if args.no_density_override and not args.enable_override:
        patient.RTstructs = []   # masks unused — saves time

    patient.import_patient_data()

    CT = patient.CTimages[0]
    # Plan is now patient.Plans[0] after import_patient_data
    Plan = patient.Plans[0]
    if not getattr(Plan, "isLoaded", 0):
        emit("ERROR", "plan failed to load (not a supported PBS ion plan?)")
        sys.exit(2)
    n_beams = len(Plan.Beams)
    log(f"CT grid={CT.GridSize} spacing={CT.PixelSpacing} IPP={CT.ImagePositionPatient}")
    log(f"plan='{Plan.PlanName}' machine='{Plan.TreatmentMachineName}' "
        f"beams={n_beams} fractions={Plan.NumberOfFractionsPlanned}")
    if n_beams == 0:
        emit("ERROR", "plan has no TREATMENT beams")
        sys.exit(2)
    if CT.GridSize[0] != CT.GridSize[1]:
        log(f"WARNING: non-square CT ({CT.GridSize[0]}x{CT.GridSize[1]}) — verify X/Y not transposed")
    progress(0.08)

    # ------------------------------------------------------------- configure
    mc2 = MCsquare()

    # Resolve BDL: 'auto' -> pick gantry vs fixed-beam model by machine name.
    # Matches the SDC convention (fixed-beam rooms use the FixedBeam model).
    bdl_name = args.bdl
    if bdl_name == "auto":
        machine = (Plan.TreatmentMachineName or "").upper()
        is_fixed = any(tok in machine for tok in ("FB", "FIXED"))
        bdl_name = args.bdl_fixed if is_fixed else args.bdl_gantry
        log(f"auto-BDL: machine='{Plan.TreatmentMachineName}' -> {bdl_name}")

    if bdl_name not in mc2.BDL.list:
        emit("ERROR", f"BDL '{bdl_name}' not found. Available: {list(mc2.BDL.list)}")
        sys.exit(2)
    if args.scanner not in mc2.Scanner.list:
        emit("ERROR", f"scanner '{args.scanner}' not found. Available: {list(mc2.Scanner.list)}")
        sys.exit(2)
    mc2.BDL.selected_BDL = bdl_name
    mc2.Scanner.selected_Scanner = args.scanner
    mc2.NumProtons = args.primaries
    mc2.MaxUncertainty = args.uncertainty
    mc2.dose2water = False
    mc2.WorkDir = str(work_dir)     # per-plan scratch (avoids ~/Work collisions)
    log(f"BDL={mc2.BDL.selected_BDL} scanner={mc2.Scanner.selected_Scanner} "
        f"primaries={mc2.NumProtons} stat_unc={mc2.MaxUncertainty}")

    # -------------------------------------------- CT density overrides (couch etc.)
    # Structures with an explicit REL_ELEC_DENSITY tag (MedPhoton/Qfix couch
    # shell+core) must be baked into the CT HU or posterior beams mis-compute
    # through the (dense carbon-fibre) couch. The TPS applies these internally;
    # MCsquare needs them in the CT. On by default; --no-density-override to skip.
    if not args.no_density_override:
        rtstruct_path = _find_rtstruct(load_dir)
        if rtstruct_path is None:
            log("density override: no RTSTRUCT found — skipping (couch not applied)")
        else:
            hu_density_file = str(install_dir / "Scanners" / args.scanner / "HU_Density_Conversion.txt")
            try:
                from ct_density_override import apply_density_overrides
                n_ov = apply_density_overrides(CT, rtstruct_path, hu_density_file, log=log)
                log(f"density override: applied to {n_ov} structure(s)")
            except Exception as e:
                import traceback
                log(f"WARNING: density override failed ({type(e).__name__}: {e}) — "
                    f"continuing on raw CT (posterior beams may be inaccurate)")
                traceback.print_exc()

    # ------------------------------------------------------------- geometry & helpers
    ps = CT.PixelSpacing
    ipp = CT.ImagePositionPatient
    spacing = np.asarray((ps[2], ps[1], ps[0]), dtype=np.float64)
    origin = np.asarray((ipp[2], ipp[1], ipp[0]), dtype=np.float64)

    def mhd_to_arr(mhd, sub_plan):
        """MCsquare MHD physical dose -> CT-aligned, RBE-weighted DoseGrid array
        (n_z, n_y, n_x), Gy(RBE). The clinical TPS RTDose is EFFECTIVE
        (RBE-weighted), so we scale physical dose by --rbe to compare like-for-like."""
        rtd = RTdose().Initialize_from_MHD(mc2.DoseName, mhd, CT, sub_plan)
        arr = np.transpose(rtd.Image, (2, 0, 1)).astype(np.float32)
        return arr * np.float32(args.rbe)

    def save_npz(arr, out_path: Path):
        tmp_path = out_path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp_path, array=arr, spacing=spacing, origin=origin)
        tmp_path.replace(out_path)

    def is_valid_npz(path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            with np.load(path) as data:
                return "array" in data and data["array"].size > 0
        except Exception:
            return False

    # --------------------------------------------------- beam numbering mapping
    # MCsquare numbers sub-plans 1..N in IonBeamSequence order, but the TPS
    # beam-level RTDose files carry the plan's own BeamNumber, which need not
    # start at 1. When the two disagree, services/gamma_analysis.py finds no
    # common beam numbers and falls back to a summed-dose comparison, losing
    # per-beam gamma. Recover the plan's numbering so the two line up.
    plan_beam_numbers = []
    try:
        import pydicom  # local: pydicom is not imported at module scope
        _pd = pydicom.dcmread(str(Plan.DcmFile), force=True)
        for _b in _pd.IonBeamSequence:
            if getattr(_b, "TreatmentDeliveryType", "TREATMENT") != "TREATMENT":
                continue
            plan_beam_numbers.append(int(_b.BeamNumber))
    except Exception as exc:  # noqa: BLE001
        log(f"WARNING: could not read plan beam numbers ({exc}); "
            f"falling back to sequential numbering")
        plan_beam_numbers = []
    if len(plan_beam_numbers) != n_beams:
        if plan_beam_numbers:
            log(f"WARNING: plan has {len(plan_beam_numbers)} TREATMENT beam(s) "
                f"but MCsquare has {n_beams}; using sequential numbering")
        plan_beam_numbers = list(range(1, n_beams + 1))
    else:
        log(f"beam numbering: MCsquare 1..{n_beams} -> plan {plan_beam_numbers}")

    # --------------------------------------------------- check cache & force flag
    if args.force:
        log("Force flag set: clearing cached beam and summed dose files")
        for f in output_dir.glob(f"{args.dose_prefix}_beam*.npz"):
            try:
                f.unlink()
            except Exception:
                pass
        summed_file = output_dir / f"{args.dose_prefix}.npz"
        if summed_file.exists():
            try:
                summed_file.unlink()
            except Exception:
                pass

    summed = None
    beams_to_simulate = []
    cached_count = 0

    for i in range(n_beams):
        beam_no = plan_beam_numbers[i]
        beam_path = output_dir / f"{args.dose_prefix}_beam{beam_no}.npz"
        if not args.force and is_valid_npz(beam_path):
            with np.load(beam_path) as data:
                arr_b = data["array"].astype(np.float32)
            summed = arr_b.copy() if summed is None else summed + arr_b
            cached_count += 1
            log(f"beam {beam_no} (field {i + 1}/{n_beams}) found in cache "
                f"(max={float(arr_b.max()):.4f} Gy) — skipping simulation")
            emit("RESULT_BEAM", beam_no, beam_path)
        else:
            beams_to_simulate.append(i)

    summed_path = output_dir / f"{args.dose_prefix}.npz"
    if cached_count > 0 and summed is not None:
        save_npz(summed, summed_path)
        emit("RESULT_SUMMED", summed_path)

    if not beams_to_simulate:
        log(f"All {n_beams} beams already computed in {output_dir} — done!")
        progress(1.0)
        emit("DONE")
        return

    log(f"Need simulation for {len(beams_to_simulate)}/{n_beams} beams "
        f"({cached_count} loaded from cache)")

    # ------------------------------------------------------- build MCsquare CT input
    log("building MCsquare input (CT.mhd, BDL)")
    mc2.init_simulation_directory()
    mc2.export_CT_for_MCsquare(CT, os.path.join(mc2.WorkDir, "CT.mhd"), mc2.Crop_CT_contour)
    mc2.BDL.import_BDL()
    progress(0.12)

    # ------------------------------------------------------------------ resolve exe
    is_win = platform.system().lower() == "windows"

    def _is_compatible(p: Path) -> bool:
        if not p.is_file() or not p.exists():
            return False
        if not is_win:
            try:
                p.chmod(p.stat().st_mode | 0o755)
            except Exception:
                pass
        try:
            proc = subprocess.run([str(p)], capture_output=True, text=True, timeout=1)
            out = (proc.stdout or "") + (proc.stderr or "")
            if "Please verify that both the operating system and the processor" in out:
                return False
            return True
        except Exception:
            return False

    exe_path = Path(args.exe)
    target_exe: Optional[Path] = None
    if exe_path.is_file() and exe_path.exists():
        target_exe = exe_path.resolve()
    elif (install_dir / args.exe).exists():
        target_exe = (install_dir / args.exe).resolve()

    if target_exe and _is_compatible(target_exe):
        exe = target_exe
    else:
        if target_exe:
            log(f"WARNING: '{target_exe.name}' is incompatible with host CPU (instruction set / vendor check failed) — scanning for compatible fallback binary...")
        fallbacks = (
            ["MCsquare_win_avx2.exe", "MCsquare_win.exe", "MCsquare_win_avx.exe", "MCsquare_win_sse4.exe"]
            if is_win
            else ["MCsquare_linux", "MCsquare_linux_avx2", "MCsquare_linux_avx", "MCsquare_linux_sse4", "MCsquare_linux_avx512"]
        )
        exe = None
        for fb in fallbacks:
            candidate = install_dir / fb
            if candidate.exists() and _is_compatible(candidate):
                log(f"Selected compatible MCsquare binary: '{fb}'")
                exe = candidate.resolve()
                break
        if exe is None:
            if target_exe:
                exe = target_exe
            else:
                emit("ERROR", f"No compatible MCsquare executable found in {install_dir}")
                sys.exit(2)

    # Ensure executable permission on Linux / Unix
    if not is_win:
        try:
            exe.chmod(exe.stat().st_mode | 0o755)
        except Exception:
            pass

    env = os.environ.copy()
    env["MCsquare_Materials_Dir"] = str(install_dir / "Materials")

    # ----------------------------------------------- incremental beam-by-beam loop
    import copy
    import re
    import signal
    pct_re = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")

    for sim_step, i in enumerate(beams_to_simulate):
        beam_no = plan_beam_numbers[i]
        beam_path = output_dir / f"{args.dose_prefix}_beam{beam_no}.npz"
        log(f"--- Simulating beam {beam_no} (field {i + 1}/{n_beams}) ---")

        # Sub-plan isolating just this beam
        sub_plan = copy.copy(Plan)
        sub_plan.Beams = [Plan.Beams[i]]

        # Clean WorkDir Outputs folder to ensure no stale MHD files are read
        outputs_dir = Path(mc2.WorkDir) / "Outputs"
        if outputs_dir.exists():
            for f in outputs_dir.glob("Dose*"):
                try:
                    f.unlink()
                except Exception:
                    pass

        # Export PlanPencil.txt for this sub-plan (computes sub_plan.DeliveredProtons)
        plan_pencil_path = os.path.join(mc2.WorkDir, "PlanPencil.txt")
        export_plan_for_MCsquare(sub_plan, plan_pencil_path, CT, mc2.BDL)
        log(f"Beam {beam_no}: DeliveredProtons={sub_plan.DeliveredProtons:.4e}")

        # Generate config for this beam
        mc2.config = generate_MCsquare_config(
            mc2.WorkDir, mc2.NumProtons, mc2.Scanner.get_path(), mc2.BDL.get_path(),
            "CT.mhd", "PlanPencil.txt", True,     # AnalyzeIndividualFields=True -> Export_Beam_dose
        )
        mc2.config["Stat_uncertainty"] = mc2.MaxUncertainty
        export_MCsquare_config(mc2.config)

        # Progress budget: 0.12 .. 0.95 split across all n_beams
        beam_base = 0.12 + 0.83 * (i / n_beams)
        beam_span = 0.83 / n_beams

        log(f"running {exe.name} for beam {beam_no} (cwd={mc2.WorkDir})")
        child = subprocess.Popen(
            [str(exe), "config.txt"],
            cwd=mc2.WorkDir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )

        def _on_signal(signum, frame):
            if child.poll() is None:
                try:
                    child.terminate()
                except Exception:
                    pass
            sys.exit(130)

        old_sigterm = signal.signal(signal.SIGTERM, _on_signal)
        old_sigint = signal.signal(signal.SIGINT, _on_signal)
        try:
            for line in child.stdout:
                line = line.rstrip()
                if not line:
                    continue
                emit("LOG", f"mc2[b{beam_no}]| " + line)
                m = pct_re.search(line)
                if m:
                    pct = min(float(m.group(1)), 100.0) / 100.0
                    frac = beam_base + beam_span * pct
                    progress(frac)
            ret = child.wait()
        finally:
            try:
                signal.signal(signal.SIGTERM, old_sigterm)
                signal.signal(signal.SIGINT, old_sigint)
            except Exception:
                pass
            if child.poll() is None:
                child.terminate()

        if ret != 0:
            emit("ERROR", f"MCsquare exited with code {ret} on beam {beam_no}")
            sys.exit(3)

        # Read the resulting dose MHD (scaled to Gy by sub_plan.DeliveredProtons)
        mhd_b = mc2.import_MCsquare_dose(sub_plan, "Dose_Beam1.mhd", args.dose_scaling)
        if mhd_b is None:
            mhd_b = mc2.import_MCsquare_dose(sub_plan, "Dose.mhd", args.dose_scaling)
        if mhd_b is None:
            emit("ERROR", f"MCsquare produced no dose output for beam {beam_no}")
            sys.exit(3)

        arr_b = mhd_to_arr(mhd_b, sub_plan)
        save_npz(arr_b, beam_path)
        emit("RESULT_BEAM", beam_no, beam_path)

        # Update running composite dose atomically
        summed = arr_b.copy() if summed is None else summed + arr_b
        save_npz(summed, summed_path)
        emit("RESULT_SUMMED", summed_path)

        log(f"beam {beam_no} completed: max={float(arr_b.max()):.4f} Gy(RBE={args.rbe:g}) "
            f"— composite updated ({i + 1}/{n_beams} beams)")
        progress(0.12 + 0.83 * ((i + 1) / n_beams))

    # All simulated beams finished
    if summed is not None:
        save_npz(summed, summed_path)
        log(f"All {n_beams} beams complete! Summed dose max={float(summed.max()):.4f} Gy shape={summed.shape}")
        emit("RESULT_SUMMED", summed_path)

    # Preserve any native openMCsquare DVH outputs from Outputs/
    outputs_dir = Path(mc2.WorkDir) / "Outputs"
    if outputs_dir.is_dir():
        for dvh_file in outputs_dir.glob("DVH_*.txt"):
            try:
                shutil.copy(str(dvh_file), str(output_dir / dvh_file.name))
                log(f"saved native MCsquare DVH: {dvh_file.name}")
            except Exception as e:
                log(f"warning copying {dvh_file.name}: {e}")

    progress(1.0)
    emit("DONE")


def _find_rtstruct(dicom_dir):
    """Return the path to the first RTSTRUCT .dcm in the store, or None."""
    import pydicom
    from pathlib import Path as _P
    for path in _P(dicom_dir).rglob("*"):
        if not path.is_file():
            continue
        try:
            dcm = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        except Exception:
            continue
        if str(getattr(dcm, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.481.3":
            return str(path)
    return None


def _apply_overrides(dicom_dir, process_parent):
    """Apply RTStruct material->water overrides in place (best-effort)."""
    import pydicom
    # DicomClass may be deployed at top level or inside Process/
    Countour_Overide = None
    for modpath in ("DicomClass", "Process.DicomClass"):
        try:
            mod = __import__(modpath, fromlist=["Countour_Overide"])
            Countour_Overide = mod.Countour_Overide
            break
        except Exception:
            continue
    if Countour_Overide is None:
        log("WARNING: DicomClass not importable — skipping water overrides")
        return

    rtstructs = []
    for path in Path(dicom_dir).rglob("*"):
        if not path.is_file():
            continue
        try:
            dcm = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        except Exception:
            continue
        if str(getattr(dcm, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.481.3":
            rtstructs.append(str(path))
    if not rtstructs:
        log("WARNING: no RTStruct found — skipping water overrides")
        return

    try:
        ov = Countour_Overide(str(dicom_dir), rtstructs)
        if not ov.Overrides:
            log("no material overrides defined in RTStruct")
            return
        for o in ov.Overrides:
            log(f"override: {o.material} on '{o.Observation_Label}'")
            ov.Edit_CT_Images(o)
        log(f"applied {len(ov.Overrides)} override(s)")
    except Exception as e:
        log(f"WARNING: override step failed ({type(e).__name__}: {e}) — continuing without it")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        emit("ERROR", f"{type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)