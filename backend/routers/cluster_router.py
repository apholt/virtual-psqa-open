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
from services.cluster.models import ClusterNode, ClusterStatus, NodeRegistrationRequest
from services.cluster.node_registry import get_node_registry

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
