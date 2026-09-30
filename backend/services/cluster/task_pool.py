"""
Task pool for Worker-Pull (outbound-only) distributed MCsquare computing.
Holds queued beam simulation tasks, manages worker leases, generates compressed
input bundles, and synchronizes task completion events with coordinator threads.
"""
from __future__ import annotations

import io
import logging
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from services.cluster.models import BeamTaskRequest

logger = logging.getLogger(__name__)


@dataclass
class QueuedTask:
    task_id: str
    req: BeamTaskRequest
    ct_mhd_bytes: bytes
    ct_raw_bytes: bytes
    output_beam_path: Path
    target_node_id: Optional[str] = None
    status: str = "pending"  # "pending", "leased", "completed", "failed", "aborted"
    leased_by: Optional[str] = None
    lease_time: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    result_bytes: Optional[bytes] = None
    max_dose: float = 0.0
    dose_shape: list[int] = field(default_factory=list)
    duration_seconds: float = 0.0
    error: Optional[str] = None
    done_event: threading.Event = field(default_factory=threading.Event)


class ClusterTaskPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[str, QueuedTask] = {}

    def enqueue_task(
        self,
        req: BeamTaskRequest,
        ct_mhd_bytes: bytes,
        ct_raw_bytes: bytes,
        output_beam_path: Path,
        target_node_id: Optional[str] = None,
    ) -> QueuedTask:
        """Enqueues a new beam simulation task for a pull worker."""
        task_id = str(uuid.uuid4())
        task = QueuedTask(
            task_id=task_id,
            req=req,
            ct_mhd_bytes=ct_mhd_bytes,
            ct_raw_bytes=ct_raw_bytes,
            output_beam_path=output_beam_path,
            target_node_id=target_node_id,
        )
        with self._lock:
            self._tasks[task_id] = task
        logger.info(
            f"Enqueued pull task {task_id}: Plan {req.plan_id} Beam {req.beam_no} "
            f"(target: {target_node_id or 'any'})"
        )
        return task

    def poll_task(self, node_id: str, is_idle: bool) -> Optional[dict]:
        """
        Called when a pull worker asks for work.
        Returns task metadata dict if a task is available, or None.
        """
        if not is_idle:
            return None

        now = time.time()
        with self._lock:
            # 1. Clean up expired leases (> 30 minutes without submission or progress)
            for t in self._tasks.values():
                if t.status == "leased" and t.lease_time and (now - t.lease_time > 1800.0):
                    logger.warning(f"Task {t.task_id} lease expired. Requeuing as pending.")
                    t.status = "pending"
                    t.leased_by = None
                    t.lease_time = None

            # 2. Find eligible pending task
            # Priority: tasks explicitly targeting this node first, then any unassigned task
            candidate: Optional[QueuedTask] = None
            for t in self._tasks.values():
                if t.status == "pending":
                    if t.target_node_id == node_id:
                        candidate = t
                        break
                    elif t.target_node_id is None and candidate is None:
                        candidate = t

            if candidate is None:
                return None

            candidate.status = "leased"
            candidate.leased_by = node_id
            candidate.lease_time = now

            req = candidate.req
            return {
                "task_id": candidate.task_id,
                "job_id": req.job_id,
                "plan_id": req.plan_id,
                "beam_no": req.beam_no,
                "field_index": req.field_index,
                "total_fields": req.total_fields,
                "delivered_protons": req.delivered_protons,
                "primaries": req.primaries,
                "uncertainty": req.uncertainty,
                "dose_scaling": req.dose_scaling,
                "rbe": req.rbe,
                "bdl_name": req.bdl_name,
                "scanner": req.scanner,
            }

    def get_task_inputs_zip(self, task_id: str) -> Optional[bytes]:
        """
        Packages CT.mhd, CT.raw, and PlanPencil.txt into an in-memory zip archive
        for fast compressed streaming to the pull worker.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("CT.mhd", task.ct_mhd_bytes)
            zf.writestr("CT.raw", task.ct_raw_bytes)
            zf.writestr("PlanPencil.txt", task.req.plan_pencil_text.encode("utf-8"))

        return buf.getvalue()

    def submit_task_result(
        self,
        task_id: str,
        dose_npz_bytes: bytes,
        max_dose: float = 0.0,
        dose_shape: Optional[list[int]] = None,
        duration_seconds: float = 0.0,
    ) -> bool:
        """Called when a worker uploads the computed dose grid result."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status not in ("leased", "pending"):
                return False

            task.result_bytes = dose_npz_bytes
            task.max_dose = max_dose
            task.dose_shape = dose_shape or []
            task.duration_seconds = duration_seconds
            task.status = "completed"
            task.done_event.set()

        logger.info(
            f"Pull task {task_id} completed by {task.leased_by} "
            f"in {duration_seconds:.1f}s (max={max_dose:.4f} Gy)"
        )
        return True

    def abort_task(self, task_id: str, reason: str = "") -> bool:
        """Called when a worker aborts a leased task (e.g. user activity)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            logger.warning(f"Pull task {task_id} aborted by worker ({reason}). Requeuing as pending.")
            task.status = "pending"
            task.leased_by = None
            task.lease_time = None
            task.error = reason
            # Do NOT set done_event yet, giving other workers or local fallback a chance to pick it up

        return True

    def fail_task(self, task_id: str, error: str) -> None:
        """Marks a task as permanently failed and wakes up coordinator."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.status = "failed"
                task.error = error
                task.done_event.set()

    def remove_task(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)


_TASK_POOL_INSTANCE: Optional[ClusterTaskPool] = None


def get_task_pool() -> ClusterTaskPool:
    global _TASK_POOL_INSTANCE
    if _TASK_POOL_INSTANCE is None:
        _TASK_POOL_INSTANCE = ClusterTaskPool()
    return _TASK_POOL_INSTANCE
