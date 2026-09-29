"""
vpsqa_worker.py — Standalone MCsquare Worker Daemon for Distributed Computing.
Runs on idle network computers to process beam-level Monte Carlo dose simulations.

Usage:
    python vpsqa_worker.py --port 8001 --mcsquare-dir C:\\path\\to\\MCsquare
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
import uvicorn

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("vpsqa_worker")

app = FastAPI(title="Virtual PSQA Worker Node", version="1.0.0")

_CURRENT_TASK: Optional[dict] = None
_RUNNING_PROC: Optional[subprocess.Popen] = None
_MCSQUARE_DIR: Path = Path("./MCsquare").resolve()
_IDLE_MINUTES: float = 5.0
_MAX_CPU_PCT: float = 30.0


def get_user_idle_seconds() -> float:
    """Returns seconds since last keyboard or mouse input."""
    sys_name = platform.system().lower()
    if sys_name == "windows":
        try:
            import ctypes
            from ctypes import Structure, c_uint, sizeof, byref

            class LASTINPUTINFO(Structure):
                _fields_ = [("cbSize", c_uint), ("dwTime", c_uint)]

            lii = LASTINPUTINFO()
            lii.cbSize = sizeof(LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(byref(lii)):
                millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
                return max(0.0, float(millis) / 1000.0)
        except Exception:
            pass
    return 999999.0


def get_cpu_percent() -> float:
    """Returns CPU utilization estimate."""
    try:
        import psutil
        return float(psutil.cpu_percent(interval=0.1))
    except ImportError:
        pass
    if hasattr(os, "getloadavg"):
        try:
            load = os.getloadavg()[0]
            cores = os.cpu_count() or 1
            return min(100.0, max(0.0, (load / cores) * 100.0))
        except Exception:
            pass
    return 10.0


def is_machine_idle() -> tuple[bool, str]:
    """Checks whether this machine is unoccupied and ready to compute."""
    idle_sec = get_user_idle_seconds()
    required_sec = _IDLE_MINUTES * 60.0

    if idle_sec < required_sec:
        return False, f"User active ({int(idle_sec)}s idle < {int(required_sec)}s required)"

    cpu_pct = get_cpu_percent()
    if cpu_pct > _MAX_CPU_PCT:
        return False, f"CPU busy ({cpu_pct:.1f}% > {_MAX_CPU_PCT:.1f}% limit)"

    return True, f"Idle ({int(idle_sec)}s user inactivity, CPU {cpu_pct:.1f}%)"


def find_mcsquare_binary(install_dir: Path) -> Path:
    """Finds a compatible MCsquare executable in the install directory."""
    is_win = platform.system().lower() == "windows"
    candidates = (
        ["MCsquare_win_avx2.exe", "MCsquare_win.exe", "MCsquare_win_avx.exe", "MCsquare_win_sse4.exe"]
        if is_win
        else ["MCsquare_linux_avx2", "MCsquare_linux", "MCsquare_linux_avx", "MCsquare_linux_sse4", "MCsquare_linux_avx512"]
    )
    for c in candidates:
        p = install_dir / c
        if p.is_file() and p.exists():
            if not is_win:
                try:
                    p.chmod(p.stat().st_mode | 0o755)
                except Exception:
                    pass
            return p.resolve()
    raise FileNotFoundError(f"No MCsquare executable found in {install_dir}")


@app.get("/status")
def get_node_status():
    global _CURRENT_TASK
    cores = os.cpu_count() or 1
    cpu = get_cpu_percent()
    idle_sec = get_user_idle_seconds()

    if _CURRENT_TASK is not None:
        status = "busy"
    else:
        idle_ok, _ = is_machine_idle()
        status = "idle" if idle_ok else "user_active"

    return {
        "status": status,
        "cores": cores,
        "cpu_pct": round(cpu, 1),
        "idle_seconds": round(idle_sec, 1),
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "active_task": _CURRENT_TASK.get("task_label") if _CURRENT_TASK else None,
        "mcsquare_dir": str(_MCSQUARE_DIR),
    }


@app.post("/simulate_beam")
async def simulate_beam(
    job_id: str = Form(...),
    plan_id: str = Form(...),
    beam_no: str = Form(...),
    delivered_protons: str = Form(...),
    primaries: str = Form(1_000_000),
    uncertainty: str = Form(1.5),
    dose_scaling: str = Form(0.9),
    rbe: str = Form(1.10),
    bdl_name: str = Form("auto"),
    scanner: str = Form("default"),
    ct_mhd: UploadFile = File(...),
    ct_raw: UploadFile = File(...),
    plan_pencil: UploadFile = File(...),
):
    """
    Executes a single beam MCsquare simulation.
    Returns the binary compressed .npz DoseGrid payload.
    """
    global _CURRENT_TASK, _RUNNING_PROC

    if _CURRENT_TASK is not None:
        raise HTTPException(status_code=409, detail="Node is currently busy with another task.")

    task_label = f"Plan {plan_id} Beam {beam_no} (Job {job_id})"
    _CURRENT_TASK = {"task_label": task_label, "start_time": time.time()}
    logger.info(f"Accepted task: {task_label}")

    with tempfile.TemporaryDirectory(prefix="vpsqa_work_") as tmpdir:
        work_path = Path(tmpdir)
        try:
            # 1. Save uploaded files
            (work_path / "CT.mhd").write_bytes(await ct_mhd.read())
            (work_path / "CT.raw").write_bytes(await ct_raw.read())
            (work_path / "PlanPencil.txt").write_bytes(await plan_pencil.read())

            # 2. Locate MCsquare binary & config files
            exe = find_mcsquare_binary(_MCSQUARE_DIR)
            scanner_dir = _MCSQUARE_DIR / "Scanners" / scanner
            if not scanner_dir.exists():
                scanner_dir = _MCSQUARE_DIR / "Scanners" / "default"

            bdl_dir = _MCSQUARE_DIR / "BDL"
            bdl_file = bdl_dir / f"{bdl_name}.txt"
            if not bdl_file.exists():
                bdl_candidates = list(bdl_dir.glob("*.txt"))
                if not bdl_candidates:
                    raise FileNotFoundError(f"No BDL files found in {bdl_dir}")
                bdl_file = bdl_candidates[0]

            # 3. Write config.txt
            config_template = f"""
[Simulation]
WorkDir = {work_path}
Num_Threads = {max(1, (os.cpu_count() or 4) - 1)}
Num_Primaries = {primaries}
Stat_uncertainty = {uncertainty}
RNG_Seed = 0
E_Cut_Pro = 0.5
D_Max = 0.2
Epsilon_Max = 0.25
Te_Min = 0.05

[Files]
CT_File = CT.mhd
ScannerDirectory = {scanner_dir}
HU_Density_Conversion_File = {scanner_dir / 'HU_Density_Conversion.txt'}
HU_Material_Conversion_File = {scanner_dir / 'HU_Material_Conversion.txt'}
BDL_Machine_Parameter_File = {bdl_file}
BDL_Plan_File = PlanPencil.txt

[Physics]
Simulate_Nuclear_Interactions = 1
Simulate_Secondary_Protons = 1
Simulate_Secondary_Deuterons = 1
Simulate_Secondary_Alphas = 1

[Outputs]
Export_Beam_dose = 1
Dose_To_Water = 0
"""
            (work_path / "config.txt").write_text(config_template.strip(), encoding="utf-8")

            # 4. Spawn MCsquare
            env = os.environ.copy()
            env["MCsquare_Materials_Dir"] = str(_MCSQUARE_DIR / "Materials")

            extra_kwargs = {}
            if platform.system().lower() == "windows":
                extra_kwargs["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
            else:
                extra_kwargs["preexec_fn"] = lambda: os.nice(10)

            logger.info(f"Running {exe.name} in {work_path}...")
            _RUNNING_PROC = subprocess.Popen(
                [str(exe), "config.txt"],
                cwd=str(work_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                **extra_kwargs,
            )

            for line in _RUNNING_PROC.stdout:
                line = line.strip()
                if line:
                    logger.debug(f"mc2[{beam_no}]| {line}")

            ret = _RUNNING_PROC.wait()
            _RUNNING_PROC = None

            if ret != 0:
                raise RuntimeError(f"MCsquare exited with code {ret}")

            # 5. Locate output dose MHD
            outputs_dir = work_path / "Outputs"
            dose_mhd_candidates = [
                outputs_dir / "Dose_Beam1.mhd",
                outputs_dir / "Dose.mhd",
            ]
            dose_mhd = next((f for f in dose_mhd_candidates if f.exists()), None)
            if not dose_mhd:
                raise FileNotFoundError(f"No dose MHD output found in {outputs_dir}")

            # Parse MHD header to extract spacing and dimensions
            headers = {}
            for line in dose_mhd.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    headers[k.strip()] = v.strip()

            dim_size = [int(x) for x in headers.get("DimSize", "0 0 0").split()]
            voxel_size = [float(x) for x in headers.get("ElementSpacing", "1 1 1").split()]
            offset = [float(x) for x in headers.get("Offset", "0 0 0").split()]
            raw_file = outputs_dir / headers.get("ElementDataFile", dose_mhd.stem + ".raw")

            raw_bytes = raw_file.read_bytes()
            dose_data = np.frombuffer(raw_bytes, dtype=np.float32).reshape(dim_size[::-1])

            # Apply DeliveredProtons scaling and RBE factor
            # MCsquare outputs dose per primary; scale to clinical Gy(RBE)
            protons = float(delivered_protons)
            scale = float(dose_scaling)
            rbe_factor = float(rbe)
            scaled_dose = dose_data * (protons * scale * rbe_factor)

            # Transpose to VPSQA convention (Z, Y, X)
            dose_z_y_x = np.transpose(scaled_dose, (2, 1, 0)).astype("<f4")

            # Bundle as npz
            buf = io.BytesIO()
            np.savez_compressed(
                buf,
                array=dose_z_y_x,
                spacing=np.array(voxel_size[::-1], dtype=np.float32),
                origin=np.array(offset[::-1], dtype=np.float32),
            )
            buf.seek(0)
            logger.info(f"Task {task_label} completed successfully. Max dose: {float(dose_z_y_x.max()):.4f} Gy")

            return Response(
                content=buf.getvalue(),
                media_type="application/octet-stream",
                headers={
                    "X-Beam-Number": str(beam_no),
                    "X-Max-Dose": str(float(dose_z_y_x.max())),
                },
            )

        except Exception as exc:
            logger.exception(f"Simulation failed: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            _CURRENT_TASK = None
            _RUNNING_PROC = None


@app.post("/abort")
def abort_current_task():
    global _RUNNING_PROC, _CURRENT_TASK
    if _RUNNING_PROC and _RUNNING_PROC.poll() is None:
        try:
            _RUNNING_PROC.terminate()
            time.sleep(0.5)
            if _RUNNING_PROC.poll() is None:
                _RUNNING_PROC.kill()
        except Exception:
            pass
    _RUNNING_PROC = None
    _CURRENT_TASK = None
    logger.info("Current task aborted.")
    return {"status": "aborted"}


def main():
    global _MCSQUARE_DIR, _IDLE_MINUTES, _MAX_CPU_PCT

    parser = argparse.ArgumentParser(description="VPSQA Standalone Distributed Worker")
    parser.add_argument("--port", type=int, default=8001, help="Port to listen on (default: 8001)")
    parser.add_argument("--host", default="0.0.0.0", help="Host IP to bind (default: 0.0.0.0)")
    parser.add_argument("--mcsquare-dir", default="./MCsquare", help="Path to MCsquare directory containing BDL/ and executable")
    parser.add_argument("--idle-minutes", type=float, default=5.0, help="Inactivity minutes before accepting tasks")
    parser.add_argument("--max-cpu-pct", type=float, default=30.0, help="Max background CPU % before considered busy")
    args = parser.parse_args()

    _MCSQUARE_DIR = Path(args.mcsquare_dir).resolve()
    _IDLE_MINUTES = args.idle_minutes
    _MAX_CPU_PCT = args.max_cpu_pct

    logger.info(f"Starting Virtual PSQA Worker on port {args.port}...")
    logger.info(f"MCsquare directory: {_MCSQUARE_DIR}")
    logger.info(f"Idle criteria: > {args.idle_minutes} min user inactivity and < {args.max_cpu_pct}% CPU")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
