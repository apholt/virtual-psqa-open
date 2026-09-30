"""
Cluster management router.
Provides endpoints for monitoring, configuring, and testing distributed MCsquare worker nodes.
"""
from __future__ import annotations

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings
from services.cluster.models import (
    ClusterNode,
    ClusterStatus,
    NodeRegistrationRequest,
    WorkerHeartbeatRequest,
    WorkerPollRequest,
    WorkerAbortRequest,
)
from services.cluster.node_registry import get_node_registry
from services.cluster.task_pool import get_task_pool
from fastapi import File, Form, Response, UploadFile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cluster", tags=["cluster"])


class ClusterToggleRequest(BaseModel):
    enabled: bool


@router.get("/status", response_model=ClusterStatus)
def get_cluster_status():
    """Returns cluster overview, total cores, active nodes, and idle states."""
    registry = get_node_registry()
    return registry.get_cluster_status()


@router.get("/nodes", response_model=list[ClusterNode])
def list_nodes():
    """Returns all registered cluster nodes with refreshed live statuses."""
    registry = get_node_registry()
    return registry.refresh_all()


@router.post("/nodes", response_model=ClusterNode)
def add_node(payload: NodeRegistrationRequest):
    """Registers a new worker node in the cluster."""
    registry = get_node_registry()
    return registry.add_node(payload)


@router.delete("/nodes/{node_id}")
def delete_node(node_id: str):
    """Removes a worker node from the cluster."""
    registry = get_node_registry()
    success = registry.remove_node(node_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return {"success": True, "message": f"Node {node_id} removed"}


@router.post("/nodes/{node_id}/test", response_model=ClusterNode)
def test_node(node_id: str):
    """Pings a specific worker node and returns its live responsiveness and capabilities."""
    registry = get_node_registry()
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return registry.ping_node(node, timeout_sec=5.0)


@router.post("/nodes/{node_id}/toggle", response_model=ClusterNode)
def toggle_node(node_id: str):
    """Enables or disables a specific worker node."""
    registry = get_node_registry()
    node = registry.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    node.enabled = not node.enabled
    registry._save_nodes()
    return node


@router.post("/toggle", response_model=ClusterStatus)
def toggle_cluster(payload: ClusterToggleRequest):
    """Enables or disables distributed cluster computation mode globally."""
    settings.CLUSTER_ENABLED = payload.enabled
    logger.info(f"Cluster computing {'enabled' if payload.enabled else 'disabled'}")
    registry = get_node_registry()
    return registry.get_cluster_status()


# ============================================================================
# Outbound Worker-Pull Endpoints (Bypasses Hospital Inbound Firewalls)
# ============================================================================


@router.post("/worker/heartbeat")
def worker_heartbeat(payload: WorkerHeartbeatRequest):
    """Outbound pull worker presence check-in."""
    registry = get_node_registry()
    node = registry.record_heartbeat(payload)
    return {
        "status": "ok",
        "node_id": node.id,
        "enabled": node.enabled,
        "cluster_enabled": settings.CLUSTER_ENABLED,
    }


@router.post("/worker/poll")
def worker_poll(payload: WorkerPollRequest):
    """Worker checks if there is any pending beam task to compute."""
    pool = get_task_pool()
    task_meta = pool.poll_task(node_id=payload.node_id, is_idle=payload.is_idle)
    return {"task": task_meta}


@router.get("/worker/task/{task_id}/inputs")
def download_task_inputs(task_id: str):
    """Streams CT geometry and PlanPencil files bundled in a compressed zip."""
    pool = get_task_pool()
    zip_bytes = pool.get_task_inputs_zip(task_id)
    if not zip_bytes:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found or expired")
    return Response(content=zip_bytes, media_type="application/zip")


@router.post("/worker/task/{task_id}/result")
async def upload_task_result(
    task_id: str,
    dose_file: UploadFile = File(...),
    max_dose: float = Form(0.0),
    duration_seconds: float = Form(0.0),
):
    """Worker uploads the completed .npz dose grid."""
    pool = get_task_pool()
    content = await dose_file.read()
    success = pool.submit_task_result(
        task_id=task_id,
        dose_npz_bytes=content,
        max_dose=max_dose,
        duration_seconds=duration_seconds,
    )
    if not success:
        raise HTTPException(status_code=400, detail="Failed to accept task result (expired or not leased)")
    return {"status": "accepted"}


@router.post("/worker/task/{task_id}/abort")
def abort_task(task_id: str, payload: WorkerAbortRequest):
    """Worker signals that calculation was aborted (e.g. user moved mouse/keyboard)."""
    pool = get_task_pool()
    success = pool.abort_task(task_id, reason=payload.reason)
    return {"status": "aborted" if success else "not_found"}

