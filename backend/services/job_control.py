"""
Cooperative and immediate job cancellation registry.

Jobs run in background threads (FastAPI BackgroundTasks). A cancel request adds
the job id to a thread-safe set; long-running runners poll is_cancelled() at
checkpoints and raise JobCancelled to abort cleanly. Subprocesses registered via
register_process() are immediately terminated upon request_cancel().
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cancelled: set[int] = set()
_processes: dict[int, subprocess.Popen] = {}


class JobCancelled(Exception):
    """Raised by a runner when its job has been cancelled."""


def register_process(job_id: int, proc: subprocess.Popen) -> None:
    """Registers an active child process for a given job ID."""
    with _lock:
        _processes[job_id] = proc


def unregister_process(job_id: int) -> None:
    """Unregisters a child process once finished."""
    with _lock:
        _processes.pop(job_id, None)


def _kill_proc(proc: subprocess.Popen) -> None:
    """Reliably terminates a process tree across Linux and Windows."""
    try:
        if proc.poll() is not None:
            return
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
    except Exception as exc:
        logger.warning(f"Error terminating process {proc.pid}: {exc}")


def request_cancel(job_id: int) -> None:
    """Flags the job as cancelled and immediately terminates any registered subprocess."""
    proc_to_kill: Optional[subprocess.Popen] = None
    with _lock:
        _cancelled.add(job_id)
        proc_to_kill = _processes.get(job_id)

    if proc_to_kill is not None:
        logger.info(f"Terminating running subprocess for cancelled job {job_id} (PID {proc_to_kill.pid})")
        _kill_proc(proc_to_kill)


def is_cancelled(job_id: int) -> bool:
    """Returns True if the job has been cancelled."""
    with _lock:
        return job_id in _cancelled


def clear(job_id: int) -> None:
    """Clears cancellation state and process registration for a job ID."""
    with _lock:
        _cancelled.discard(job_id)
        _processes.pop(job_id, None)
