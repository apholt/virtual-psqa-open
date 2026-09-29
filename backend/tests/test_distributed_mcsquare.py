"""
Tests for distributed MCsquare compute cluster, node registry, idle detector,
and automatic failover to local computation.
"""
from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
import uvicorn

from config import settings
from services.auth_service import create_session_token
from services.cluster.idle_detector import (
    get_cpu_percent,
    get_user_idle_seconds,
    is_machine_idle,
)
from services.cluster.models import (
    BeamTaskRequest,
    ClusterNode,
    NodeRegistrationRequest,
)
from services.cluster.node_registry import NodeRegistry
from services.cluster.coordinator import ClusterCoordinator
from main import app as main_app


@pytest.fixture
def temp_registry(tmp_path):
    storage_file = tmp_path / "test_cluster_nodes.json"
    registry = NodeRegistry(storage_path=storage_file)
    return registry


def test_idle_detector_metrics():
    """Verify idle detector returns sane non-crashing numbers cross-platform."""
    idle_sec = get_user_idle_seconds()
    assert isinstance(idle_sec, float)
    assert idle_sec >= 0.0

    cpu = get_cpu_percent(sample_interval=0.05)
    assert isinstance(cpu, float)
    assert 0.0 <= cpu <= 100.0

    is_idle, reason = is_machine_idle(idle_minutes_threshold=0.001, max_cpu_percent=100.0)
    assert isinstance(is_idle, bool)
    assert isinstance(reason, str)


def test_node_registry_crud(temp_registry):
    """Verify adding, listing, removing, and toggling nodes in the registry."""
    # Initially empty
    assert len(temp_registry.list_nodes()) == 0

    # Add node
    req = NodeRegistrationRequest(
        name="Physics-Workstation-1",
        url="http://192.168.1.150:8001",
        enabled=True,
    )
    node = temp_registry.add_node(req)
    assert node.id == "node-1"
    assert node.name == "Physics-Workstation-1"
    assert node.url == "http://192.168.1.150:8001"

    # List
    nodes = temp_registry.list_nodes()
    assert len(nodes) == 1
    assert nodes[0].id == "node-1"

    # Status summary
    status = temp_registry.get_cluster_status()
    assert status.total_nodes == 1
    assert status.online_nodes == 0  # 192.168.1.150 is unreachable in test

    # Remove node
    deleted = temp_registry.remove_node("node-1")
    assert deleted is True
    assert len(temp_registry.list_nodes()) == 0


def test_cluster_router_endpoints():
    """Test FastAPI /api/cluster endpoints."""
    token = create_session_token("admin")
    client = TestClient(main_app, cookies={settings.AUTH_SESSION_COOKIE: token})

    # 1. Get cluster status
    resp = client.get("/api/cluster/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "enabled" in data
    assert "nodes" in data

    # 2. Toggle cluster
    resp = client.post("/api/cluster/toggle", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    # 3. Add node
    resp = client.post(
        "/api/cluster/nodes",
        json={"name": "Test-PC", "url": "http://localhost:9999", "enabled": True},
    )
    assert resp.status_code == 200
    node_id = resp.json()["id"]

    # 4. List nodes
    resp = client.get("/api/cluster/nodes")
    assert resp.status_code == 200
    nodes = resp.json()
    assert any(n["id"] == node_id for n in nodes)

    # 5. Delete node
    del_resp = client.delete(f"/api/cluster/nodes/{node_id}")
    assert del_resp.status_code == 200

    # Reset setting
    client.post("/api/cluster/toggle", json={"enabled": False})


class MockWorkerServer:
    """Spins up a lightweight mock worker node on a background thread for testing."""

    def __init__(self, port: int, should_fail: bool = False):
        self.port = port
        self.should_fail = should_fail
        self.server = None
        self.thread = None

        self.mock_app = FastAPI()

        @self.mock_app.get("/status")
        def status():
            return {
                "status": "busy" if self.should_fail else "idle",
                "cores": 16,
                "cpu_pct": 5.0,
                "idle_seconds": 600.0,
                "hostname": f"mock-worker-{self.port}",
                "os": "Test OS",
            }

        @self.mock_app.post("/simulate_beam")
        def simulate_beam():
            if self.should_fail:
                raise HTTPException(status_code=500, detail="Mock worker node simulated error")

            # Generate synthetic dose grid payload
            dose = np.ones((10, 10, 10), dtype=np.float32) * 2.5
            buf = io.BytesIO()
            np.savez_compressed(
                buf,
                array=dose,
                spacing=np.array([2.0, 2.0, 2.0], dtype=np.float32),
                origin=np.array([0.0, 0.0, 0.0], dtype=np.float32),
            )
            buf.seek(0)
            return Response(content=buf.getvalue(), media_type="application/octet-stream")

    def start(self):
        config = uvicorn.Config(self.mock_app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        time.sleep(0.5)

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=2.0)


def test_cluster_coordinator_remote_dispatch_and_sum(tmp_path, monkeypatch):
    """
    Spins up a mock worker node on port 8945, registers it,
    dispatches a beam task, and verifies dose npz receipt.
    """
    mock_port = 8945
    mock_server = MockWorkerServer(port=mock_port, should_fail=False)
    mock_server.start()

    try:
        coordinator = ClusterCoordinator()
        node = ClusterNode(
            id="mock-1",
            name="Mock-Worker",
            url=f"http://127.0.0.1:{mock_port}",
            enabled=True,
            is_online=True,
            is_idle=True,
            cores=16,
        )

        req = BeamTaskRequest(
            job_id=999,
            plan_id=101,
            beam_no=1,
            field_index=0,
            total_fields=1,
            plan_pencil_text="MOCK_PENCIL",
            delivered_protons=1.0e9,
        )

        out_beam_path = tmp_path / "mc_dose_beam1.npz"
        res = coordinator.dispatch_remote_beam(
            node=node,
            req=req,
            ct_mhd_bytes=b"MOCK_MHD",
            ct_raw_bytes=b"MOCK_RAW",
            output_beam_path=out_beam_path,
        )

        assert res.success is True
        assert res.beam_no == 1
        assert res.max_dose == 2.5
        assert out_beam_path.exists()

        # Check beam file on disk
        with np.load(out_beam_path) as z:
            assert z["array"].shape == (10, 10, 10)
            assert np.allclose(z["array"], 2.5)

        # Test beam summing helper
        summed_path = coordinator._sum_existing_beams(tmp_path, [1])
        assert Path(summed_path).exists()
        with np.load(summed_path) as z:
            assert np.allclose(z["array"], 2.5)

    finally:
        mock_server.stop()


def test_cluster_coordinator_failure_handling(tmp_path):
    """Verifies that an erroring remote worker raises an exception caught for fallback."""
    mock_port = 8946
    mock_server = MockWorkerServer(port=mock_port, should_fail=True)
    mock_server.start()

    try:
        coordinator = ClusterCoordinator()
        node = ClusterNode(
            id="mock-fail",
            name="Mock-Failing-Worker",
            url=f"http://127.0.0.1:{mock_port}",
            enabled=True,
        )

        req = BeamTaskRequest(
            job_id=999,
            plan_id=101,
            beam_no=2,
            field_index=1,
            total_fields=2,
            plan_pencil_text="MOCK_PENCIL",
            delivered_protons=1.0e9,
        )

        out_path = tmp_path / "mc_dose_beam2.npz"
        with pytest.raises(RuntimeError) as exc_info:
            coordinator.dispatch_remote_beam(
                node=node,
                req=req,
                ct_mhd_bytes=b"MOCK_MHD",
                ct_raw_bytes=b"MOCK_RAW",
                output_beam_path=out_path,
            )

        assert "500" in str(exc_info.value)
        assert not out_path.exists()

    finally:
        mock_server.stop()
