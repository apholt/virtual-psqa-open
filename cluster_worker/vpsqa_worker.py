"""
vpsqa_worker.py — Standalone MCsquare Worker Daemon for Distributed Computing.
Runs on idle network computers to process beam-level Monte Carlo dose simulations.

Supports two operational modes:
1. PULL Mode (Recommended for hospital networks):
   Connects OUTBOUND to the Virtual PSQA server. Bypasses hospital inbound firewalls.
   Usage:
       python vpsqa_worker.py --server-url http://172.20.145.65:8003

2. PUSH Mode (Legacy / flat LANs):
   Listens on port 8001 for inbound HTTP simulation requests.
   Usage:
       python vpsqa_worker.py --port 8001
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
import zipfile
from pathlib import Path
from typing import Optional

import httpx
import numpy as np

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("vpsqa_worker")

_CURRENT_TASK: Optional[dict] = None
_RUNNING_PROC: Optional[subprocess.Popen] = None
_MCSQUARE_DIR: Path = Path("./MCsquare").resolve()
_IDLE_MINUTES: float = 5.0
_MAX_CPU_PCT: float = 30.0
_IDLE_OVERRIDDEN_BY_CLI: bool = False
_MAX_CPU_OVERRIDDEN_BY_CLI: bool = False


class AbortCalculationException(Exception):
    pass


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
    return 3600.0


def get_cpu_percent() -> float:
    """Returns system CPU utilization percentage."""
    sys_name = platform.system().lower()
    if sys_name == "windows":
        try:
            cmd = "wmic cpu get loadpercentage /value"
            out = subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if "LoadPercentage=" in line:
                    return float(line.split("=")[1].strip())
        except Exception:
            pass
    elif sys_name == "linux":
        try:
            with open("/proc/stat", "r") as f:
                fields = [float(x) for x in f.readline().strip().split()[1:5]]
            idle1 = fields[3]
            total1 = sum(fields)
            time.sleep(0.1)
            with open("/proc/stat", "r") as f:
                fields = [float(x) for x in f.readline().strip().split()[1:5]]
            idle2 = fields[3]
            total2 = sum(fields)
            diff_idle = idle2 - idle1
            diff_total = total2 - total1
            return max(0.0, min(100.0, (1.0 - diff_idle / max(1.0, diff_total)) * 100.0))
        except Exception:
            pass
    return 10.0


def is_machine_idle() -> tuple[bool, str]:
    """Checks whether this machine is unoccupied and ready to compute."""
    cpu_pct = get_cpu_percent()
    if _IDLE_MINUTES <= 0:
        return True, f"Dedicated mode (always active, CPU {cpu_pct:.1f}%)"

    idle_sec = get_user_idle_seconds()
    required_sec = _IDLE_MINUTES * 60.0
    if idle_sec < required_sec:
        return False, f"User active ({int(idle_sec)}s idle < {int(required_sec)}s required)"

    if _MAX_CPU_PCT > 0 and cpu_pct > _MAX_CPU_PCT:
        return False, f"CPU busy ({cpu_pct:.1f}% > {_MAX_CPU_PCT:.1f}% limit)"

    status_str = f"Idle ({int(idle_sec)}s user inactivity, CPU {cpu_pct:.1f}%)"
    return True, status_str


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


def execute_mc2_simulation(
    work_path: Path,
    meta: dict,
    beam_label: str = "",
) -> tuple[bytes, float, list[int]]:
    """
    Executes MCsquare simulation inside work_path.
    Monitors user activity and aborts if user becomes active on this workstation.
    Returns (npz_dose_bytes, max_dose, dose_shape).
    """
    global _RUNNING_PROC

    exe = find_mcsquare_binary(_MCSQUARE_DIR)
    scanner = meta.get("scanner", "default")
    scanner_dir = _MCSQUARE_DIR / "Scanners" / scanner
    if not scanner_dir.exists():
        scanner_dir = _MCSQUARE_DIR / "Scanners" / "default"

    bdl_name = meta.get("bdl_name", "auto")
    bdl_dir = _MCSQUARE_DIR / "BDL"
    bdl_file = bdl_dir / f"{bdl_name}.txt"
    if not bdl_file.exists():
        bdl_candidates = list(bdl_dir.glob("*.txt"))
        if not bdl_candidates:
            raise FileNotFoundError(f"No BDL files found in {bdl_dir}")
        bdl_file = bdl_candidates[0]

    primaries = meta.get("primaries", 1_000_000)
    uncertainty = meta.get("uncertainty", 1.5)

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

    # Monitor loop: check process output AND check for user activity
    while True:
        ret = _RUNNING_PROC.poll()
        if ret is not None:
            break

        # Check if user touched keyboard or mouse (only if idle requirement is active)
        if _IDLE_MINUTES > 0:
            idle_sec = get_user_idle_seconds()
            if idle_sec < 4.0:
                logger.warning(f"User activity detected on workstation ({idle_sec:.1f}s ago). Aborting MCsquare to yield CPU.")
                try:
                    _RUNNING_PROC.terminate()
                    time.sleep(0.3)
                    if _RUNNING_PROC.poll() is None:
                        _RUNNING_PROC.kill()
                except Exception:
                    pass
                _RUNNING_PROC = None
                raise AbortCalculationException("User activity detected on worker PC")

        time.sleep(0.5)

    ret = _RUNNING_PROC.wait()
    _RUNNING_PROC = None

    if ret != 0:
        raise RuntimeError(f"MCsquare exited with code {ret}")

    # Locate output dose MHD
    outputs_dir = work_path / "Outputs"
    dose_mhd_candidates = [
        outputs_dir / "Dose_Beam1.mhd",
        outputs_dir / "Dose.mhd",
    ]
    dose_mhd = next((f for f in dose_mhd_candidates if f.exists()), None)
    if not dose_mhd:
        raise FileNotFoundError(f"No dose MHD output found in {outputs_dir}")

    # Parse MHD header
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

    # Apply scaling
    protons = float(meta.get("delivered_protons", 1.0))
    scale = float(meta.get("dose_scaling", 0.9))
    rbe_factor = float(meta.get("rbe", 1.10))
    scaled_dose = dose_data * (protons * scale * rbe_factor)

    # Transpose to VPSQA convention (Z, Y, X)
    dose_z_y_x = np.transpose(scaled_dose, (2, 1, 0)).astype("<f4")
    max_dose = float(dose_z_y_x.max())
    dose_shape = list(dose_z_y_x.shape)

    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        array=dose_z_y_x,
        spacing=np.array(voxel_size[::-1], dtype=np.float32),
        origin=np.array(offset[::-1], dtype=np.float32),
    )
    return buf.getvalue(), max_dose, dose_shape


def execute_task_from_zip(
    task_id: str,
    zip_bytes: bytes,
    meta: dict,
) -> tuple[bytes, float, list[int]]:
    """Unpacks task input zip into temporary directory and runs simulation."""
    with tempfile.TemporaryDirectory(prefix="vpsqa_pull_") as tmpdir:
        work_path = Path(tmpdir)
        with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
            zf.extractall(work_path)

        beam_label = f"Plan {meta.get('plan_id')} Beam {meta.get('beam_no')}"
        return execute_mc2_simulation(work_path, meta, beam_label=beam_label)


# ============================================================================
# WORKER-PULL ENGINE (Outbound-only, bypasses hospital firewalls)
# ============================================================================


def run_pull_worker(
    server_url: str,
    node_id: str,
    name: Optional[str] = None,
    poll_interval: float = 3.0,
):
    global _IDLE_MINUTES, _MAX_CPU_PCT, _CURRENT_TASK
    import urllib.parse

    clean_server = server_url.strip().strip("'\"").rstrip("/")
    if not clean_server.startswith("http://") and not clean_server.startswith("https://"):
        clean_server = f"http://{clean_server}"

    # Check if a port was specified
    parsed = urllib.parse.urlsplit(clean_server)
    if not parsed.port:
        logger.warning(
            f"[NOTE] No port specified in server URL '{clean_server}'. "
            f"If your Virtual PSQA server runs on a specific port (e.g. :8003 or :8000), "
            f"be sure to include it: {clean_server}:8003"
        )

    worker_name = name or socket.gethostname()
    logger.info("=================================================================")
    logger.info("  Virtual PSQA Distributed MCsquare Worker — PULL (Outbound) Mode")
    logger.info("=================================================================")
    logger.info(f"Target Server URL: {clean_server}")
    logger.info(f"Worker Node ID:   {node_id} ({worker_name})")
    logger.info(f"Host Machine:     {socket.gethostname()} ({platform.system()} {platform.release()})")
    logger.info(f"MCsquare Home:    {_MCSQUARE_DIR}")
    if _IDLE_MINUTES <= 0:
        logger.info(f"Idle Requirement: Dedicated mode (always active, 0 min idle requirement)")
    else:
        logger.info(f"Idle Requirement: > {_IDLE_MINUTES} min user inactivity and < {_MAX_CPU_PCT}% background CPU")
    logger.info("=================================================================")

    # Test initial connection synchronously so errors are immediately visible in the console
    logger.info(f"Connecting to Virtual PSQA Server at {clean_server}...")
    for attempt_url in ([clean_server, clean_server.replace("http://", "https://", 1)] if clean_server.startswith("http://") else [clean_server]):
        try:
            init_idle, _ = is_machine_idle()
            init_payload = {
                "node_id": node_id,
                "name": worker_name,
                "hostname": socket.gethostname(),
                "os": f"{platform.system()} {platform.release()}",
                "cores": os.cpu_count() or 1,
                "cpu_pct": round(get_cpu_percent(), 1),
                "idle_seconds": round(get_user_idle_seconds(), 1),
                "is_idle": init_idle,
                "status": "idle" if init_idle else "user_active",
                "mode": "pull",
            }
            with httpx.Client(timeout=8.0, verify=False) as client:
                resp = client.post(f"{attempt_url}/api/cluster/worker/heartbeat", json=init_payload)
                if resp.status_code == 200:
                    hb_data = resp.json()
                    clean_server = attempt_url
                    logger.info(
                        f">>> [CONNECTED] Successfully registered with Virtual PSQA Server at {clean_server}!\n"
                        f"    Node ID: {node_id} | Status: {init_payload['status']} | Presence confirmed."
                    )
                    break
                else:
                    logger.error(
                        f"[ERROR] Server responded with HTTP {resp.status_code}: {resp.text}\n"
                        f"Check that the server URL ({attempt_url}) is correct."
                    )
        except Exception as exc:
            if attempt_url == clean_server and clean_server.startswith("http://"):
                logger.info(f"HTTP connection to {attempt_url} failed ({exc}). Retrying with HTTPS...")
                continue
            logger.error(
                f"[CONNECTION ERROR] Failed to reach Virtual PSQA Server at {clean_server}!\n"
                f"Details: {exc}\n\n"
                f"Please check:\n"
                f" 1. Is the port correct? (Current URL: {clean_server})\n"
                f" 2. Is the server running?\n"
                f" 3. Is the firewall blocking the port on the server?\n"
                f"    - On Linux server: run 'sudo ufw allow 8000/tcp' (or appropriate port)\n"
                f"    - On Windows server: run 'New-NetFirewallRule -DisplayName \"Virtual PSQA\" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow'\n"
                f" 4. If the server has SSL/HTTPS enabled, ensure the URL uses https://\n"
                f" To change the saved server URL, run:\n"
                f"    run_worker.bat <new_url>\n"
                f" or edit cluster_worker/server_url.txt directly."
            )

    import threading

    def heartbeat_worker():
        global _IDLE_MINUTES, _MAX_CPU_PCT, _CURRENT_TASK
        while True:
            try:
                idle_ok, _ = is_machine_idle()
                cores = os.cpu_count() or 1
                cpu = get_cpu_percent()
                idle_sec = get_user_idle_seconds()

                if _CURRENT_TASK is not None:
                    status = "busy"
                    is_idle_val = False
                else:
                    status = "idle" if idle_ok else "user_active"
                    is_idle_val = idle_ok

                hb_payload = {
                    "node_id": node_id,
                    "name": worker_name,
                    "hostname": socket.gethostname(),
                    "os": f"{platform.system()} {platform.release()}",
                    "cores": cores,
                    "cpu_pct": round(cpu, 1),
                    "idle_seconds": round(idle_sec, 1),
                    "is_idle": is_idle_val,
                    "status": status,
                    "mode": "pull",
                }
                with httpx.Client(timeout=5.0, verify=False) as client:
                    resp = client.post(f"{clean_server}/api/cluster/worker/heartbeat", json=hb_payload)
                    if resp.status_code == 200:
                        hb_data = resp.json()
                        if not _IDLE_OVERRIDDEN_BY_CLI and "idle_minutes" in hb_data:
                            s_idle = float(hb_data["idle_minutes"])
                            if s_idle != _IDLE_MINUTES:
                                logger.info(f"Adopted server idle threshold: {s_idle} min")
                                _IDLE_MINUTES = s_idle
                        if not _MAX_CPU_OVERRIDDEN_BY_CLI and "max_cpu_pct" in hb_data:
                            s_cpu = float(hb_data["max_cpu_pct"])
                            if s_cpu != _MAX_CPU_PCT:
                                logger.info(f"Adopted server max CPU limit: {s_cpu}%")
                                _MAX_CPU_PCT = s_cpu
                    else:
                        logger.error(f"[HEARTBEAT ERROR] Server returned HTTP {resp.status_code}: {resp.text}")
            except Exception as hb_err:
                logger.warning(f"[HEARTBEAT ERROR] Lost connection to server at {clean_server}: {hb_err}")
            time.sleep(5.0)

    hb_thread = threading.Thread(target=heartbeat_worker, daemon=True)
    hb_thread.start()

    while True:
        try:
            idle_ok, idle_reason = is_machine_idle()

            # Poll for beam tasks when idle and not busy
            if idle_ok and _CURRENT_TASK is None:
                poll_payload = {
                    "node_id": node_id,
                    "is_idle": True,
                }
                try:
                    with httpx.Client(timeout=10.0, verify=False) as client:
                        resp = client.post(f"{clean_server}/api/cluster/worker/poll", json=poll_payload)
                        if resp.status_code == 200:
                            data = resp.json()
                            task_meta = data.get("task")
                            if task_meta:
                                task_id = task_meta["task_id"]
                                beam_no = task_meta.get("beam_no")
                                plan_id = task_meta.get("plan_id")
                                task_label = f"Plan {plan_id} Beam {beam_no}"
                                _CURRENT_TASK = {"task_label": task_label, "task_id": task_id, "start_time": time.time()}
                                logger.info(f">>> [LEASED] Received Beam {beam_no} for Plan {plan_id} (Task {task_id[:8]}...)")

                                try:
                                    # Download input bundle
                                    in_resp = client.get(
                                        f"{clean_server}/api/cluster/worker/task/{task_id}/inputs",
                                        timeout=120.0,
                                    )
                                    if in_resp.status_code != 200:
                                        logger.error(f"Failed to download task inputs: {in_resp.text}")
                                        continue

                                    # Execute simulation
                                    t_start = time.time()
                                    try:
                                        dose_bytes, max_d, d_shape = execute_task_from_zip(
                                            task_id, in_resp.content, task_meta
                                        )
                                        duration = time.time() - t_start

                                        # Upload result
                                        files = {"dose_file": ("mc_dose.npz", dose_bytes, "application/octet-stream")}
                                        form_data = {"max_dose": str(max_d), "duration_seconds": str(duration)}
                                        up_resp = client.post(
                                            f"{clean_server}/api/cluster/worker/task/{task_id}/result",
                                            data=form_data,
                                            files=files,
                                            timeout=60.0,
                                        )
                                        if up_resp.status_code == 200:
                                            logger.info(
                                                f">>> [SUCCESS] Beam {beam_no} finished in {duration:.1f}s "
                                                f"(max dose: {max_d:.4f} Gy). Uploaded to server!"
                                            )
                                        else:
                                            logger.error(f"Failed to upload dose result: {up_resp.text}")

                                    except AbortCalculationException:
                                        logger.warning(f"[ABORTED] User active on workstation. Aborted task {task_id[:8]}.")
                                        client.post(
                                            f"{clean_server}/api/cluster/worker/task/{task_id}/abort",
                                            json={"task_id": task_id, "node_id": node_id, "reason": "user_active"},
                                            timeout=5.0,
                                        )
                                    except Exception as sim_err:
                                        logger.exception(f"[ERROR] Simulation failed for task {task_id[:8]}: {sim_err}")
                                        client.post(
                                            f"{clean_server}/api/cluster/worker/task/{task_id}/abort",
                                            json={"task_id": task_id, "node_id": node_id, "reason": str(sim_err)},
                                            timeout=5.0,
                                        )
                                finally:
                                    _CURRENT_TASK = None
                except Exception as poll_err:
                    logger.warning(f"Task poll failed: {poll_err}")

            time.sleep(poll_interval)

        except Exception as loop_err:
            logger.debug(f"Pull worker loop error: {loop_err}")
            time.sleep(poll_interval)


# ============================================================================
# LEGACY WORKER-PUSH SERVER (Inbound HTTP listening socket)
# ============================================================================

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
import uvicorn

app = FastAPI(title="Virtual PSQA Worker Node", version="1.0.0")


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
        "mode": "push",
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
    global _CURRENT_TASK
    if _CURRENT_TASK is not None:
        raise HTTPException(status_code=409, detail="Node is currently busy with another task.")

    task_label = f"Plan {plan_id} Beam {beam_no} (Job {job_id})"
    _CURRENT_TASK = {"task_label": task_label, "start_time": time.time()}

    meta = {
        "job_id": job_id,
        "plan_id": plan_id,
        "beam_no": beam_no,
        "delivered_protons": delivered_protons,
        "primaries": primaries,
        "uncertainty": uncertainty,
        "dose_scaling": dose_scaling,
        "rbe": rbe,
        "bdl_name": bdl_name,
        "scanner": scanner,
    }

    with tempfile.TemporaryDirectory(prefix="vpsqa_push_") as tmpdir:
        work_path = Path(tmpdir)
        (work_path / "CT.mhd").write_bytes(await ct_mhd.read())
        (work_path / "CT.raw").write_bytes(await ct_raw.read())
        (work_path / "PlanPencil.txt").write_bytes(await plan_pencil.read())

        try:
            dose_bytes, max_dose, _ = execute_mc2_simulation(work_path, meta, beam_label=task_label)
            return Response(
                content=dose_bytes,
                media_type="application/octet-stream",
                headers={
                    "X-Beam-Number": str(beam_no),
                    "X-Max-Dose": str(max_dose),
                },
            )
        finally:
            _CURRENT_TASK = None


def main():
    global _MCSQUARE_DIR, _IDLE_MINUTES, _MAX_CPU_PCT, _IDLE_OVERRIDDEN_BY_CLI, _MAX_CPU_OVERRIDDEN_BY_CLI

    parser = argparse.ArgumentParser(description="VPSQA Standalone Distributed Worker")
    parser.add_argument("--server-url", default=None, help="Virtual PSQA Server URL (e.g. http://172.20.145.65:8003). Runs in outbound PULL mode.")
    parser.add_argument("--node-id", default=None, help="Unique node ID (default: hostname)")
    parser.add_argument("--name", default=None, help="Display name for this workstation")
    parser.add_argument("--port", type=int, default=8001, help="Port to listen on in PUSH mode (default: 8001)")
    parser.add_argument("--host", default="0.0.0.0", help="Host IP to bind in PUSH mode (default: 0.0.0.0)")
    parser.add_argument("--mcsquare-dir", default="./MCsquare", help="Path to MCsquare directory containing BDL/ and executable")
    parser.add_argument("--idle-minutes", type=float, default=None, help="Inactivity minutes before accepting tasks (0 = dedicated mode)")
    parser.add_argument("--max-cpu-pct", type=float, default=None, help="Max background CPU percent before considered busy")
    args = parser.parse_args()

    _MCSQUARE_DIR = Path(args.mcsquare_dir).resolve()
    if args.idle_minutes is not None:
        _IDLE_MINUTES = args.idle_minutes
        _IDLE_OVERRIDDEN_BY_CLI = True
    if args.max_cpu_pct is not None:
        _MAX_CPU_PCT = args.max_cpu_pct
        _MAX_CPU_OVERRIDDEN_BY_CLI = True

    node_id = args.node_id or socket.gethostname()
    name = args.name or socket.gethostname()

    if args.server_url:
        # PULL Mode (Outbound only, hospital firewall friendly)
        run_pull_worker(
            server_url=args.server_url,
            node_id=node_id,
            name=name,
        )
    else:
        # PUSH Mode (Legacy inbound server)
        logger.info(f"Starting Virtual PSQA Worker in PUSH mode on port {args.port}...")
        logger.info(f"MCsquare directory: {_MCSQUARE_DIR}")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
