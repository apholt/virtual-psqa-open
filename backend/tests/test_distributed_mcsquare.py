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


def test_worker_pull_workflow(tmp_path):
    """Verifies the outbound Worker-Pull architecture: heartbeat, poll, download, result upload."""
    import zipfile
    import concurrent.futures

    token = create_session_token("admin")
    client = TestClient(main_app, cookies={settings.AUTH_SESSION_COOKIE: token})

    node_id = "test-pull-pc"
    # 1. Send heartbeat
    hb_resp = client.post(
        "/api/cluster/worker/heartbeat",
        json={
            "node_id": node_id,
            "name": "Clinical-Workstation-Pull",
            "hostname": "PCFAPPL2.TNONC.com",
            "os": "Windows 10",
            "cores": 16,
            "cpu_pct": 5.0,
            "idle_seconds": 120.0,
            "is_idle": True,
            "mode": "pull",
        },
    )
    assert hb_resp.status_code == 200
    assert hb_resp.json()["status"] == "ok"

    # 2. Check node appears in registry
    nodes_resp = client.get("/api/cluster/nodes")
    assert nodes_resp.status_code == 200
    nodes = nodes_resp.json()
    pull_node = next((n for n in nodes if n["id"] == node_id), None)
    assert pull_node is not None
    assert pull_node["is_online"] is True
    assert pull_node["mode"] == "pull"
    assert pull_node["cores"] == 16

    # 3. Simulate coordinator dispatching a beam in a background thread
    coordinator = ClusterCoordinator()
    c_node = ClusterNode(**pull_node)
    req = BeamTaskRequest(
        job_id=888,
        plan_id=102,
        beam_no=1,
        field_index=0,
        total_fields=1,
        plan_pencil_text="MOCK_PENCIL_DATA",
        delivered_protons=2.5e9,
    )
    out_beam_path = tmp_path / "mc_dose_beam1_pull.npz"

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(
            coordinator.dispatch_remote_beam,
            node=c_node,
            req=req,
            ct_mhd_bytes=b"MOCK_CT_MHD",
            ct_raw_bytes=b"MOCK_CT_RAW",
            output_beam_path=out_beam_path,
            timeout_seconds=30.0,
        )

        time.sleep(0.1)

        # 4. Worker polls for task
        poll_resp = client.post("/api/cluster/worker/poll", json={"node_id": node_id, "is_idle": True})
        assert poll_resp.status_code == 200
        data = poll_resp.json()
        assert data["task"] is not None
        task_meta = data["task"]
        task_id = task_meta["task_id"]
        assert task_meta["plan_id"] == 102
        assert task_meta["beam_no"] == 1

        # 5. Worker downloads input bundle
        inputs_resp = client.get(f"/api/cluster/worker/task/{task_id}/inputs")
        assert inputs_resp.status_code == 200
        # Verify zip contents
        zf = zipfile.ZipFile(io.BytesIO(inputs_resp.content))
        namelist = zf.namelist()
        assert "CT.mhd" in namelist
        assert "CT.raw" in namelist
        assert "PlanPencil.txt" in namelist
        assert zf.read("PlanPencil.txt").decode("utf-8") == "MOCK_PENCIL_DATA"

        # 6. Worker creates mock dose and uploads result
        mock_dose = np.ones((8, 8, 8), dtype=np.float32) * 3.14
        buf = io.BytesIO()
        np.savez_compressed(buf, array=mock_dose)
        buf.seek(0)

        up_resp = client.post(
            f"/api/cluster/worker/task/{task_id}/result",
            data={"max_dose": "3.14", "duration_seconds": "1.2"},
            files={"dose_file": ("mc_dose.npz", buf.getvalue(), "application/octet-stream")},
        )
        assert up_resp.status_code == 200

        # 7. Coordinator future resolves
        res = future.result(timeout=5.0)
        assert res.success is True
        assert res.beam_no == 1
        assert res.max_dose == 3.14
        assert out_beam_path.exists()

        with np.load(out_beam_path) as z:
            assert np.allclose(z["array"], 3.14)


def test_worker_pull_unauthenticated_access():
    """Verify that remote daemon can access worker endpoints without a browser login session."""
    orig_auth = settings.AUTH_ENABLED
    try:
        settings.AUTH_ENABLED = True
        unauthenticated_client = TestClient(main_app)
        resp = unauthenticated_client.post(
            "/api/cluster/worker/heartbeat",
            json={
                "node_id": "daemon-no-cookie",
                "name": "Daemon No Cookie",
                "cores": 4,
                "cpu_pct": 2.0,
                "idle_seconds": 600.0,
                "is_idle": True,
                "mode": "pull",
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["status"] == "ok"
    finally:
        settings.AUTH_ENABLED = orig_auth


