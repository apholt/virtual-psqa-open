"""
Cluster Node Registry.
Tracks available remote compute nodes, monitors health, checks idle status,
and provides node discovery for distributed MCsquare calculations.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import httpx

from config import settings
from services.cluster.models import (
    ClusterNode,
    ClusterStatus,
    NodeRegistrationRequest,
    WorkerHeartbeatRequest,
)

logger = logging.getLogger(__name__)

_DEFAULT_STORAGE_FILE = Path(settings.RESULTS_PATH).parent / "cluster_nodes.json"


class NodeRegistry:
    def __init__(self, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or _DEFAULT_STORAGE_FILE
        self._nodes: dict[str, ClusterNode] = {}
        self._load_nodes()

    def _load_nodes(self) -> None:
        """Loads nodes from persistent JSON storage and config settings."""
        # 1. Load from settings.CLUSTER_NODES first
        for idx, url in enumerate(settings.CLUSTER_NODES):
            clean_url = url.rstrip("/")
            node_id = f"node-{idx + 1}"
            self._nodes[node_id] = ClusterNode(
                id=node_id,
                name=f"Worker {idx + 1}",
                url=clean_url,
                enabled=True,
            )

        # 2. Overlay from storage file if it exists
        if self.storage_path.exists():
            try:
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                for item in data:
                    node = ClusterNode(**item)
                    self._nodes[node.id] = node
            except Exception as exc:
                logger.warning(f"Could not load cluster nodes from {self.storage_path}: {exc}")

    def _save_nodes(self) -> None:
        """Persists registered nodes to disk."""
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            serializable = [n.model_dump() for n in self._nodes.values()]
            self.storage_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"Failed to persist cluster nodes: {exc}")

    def list_nodes(self) -> list[ClusterNode]:
        return list({n.id: n for n in self._nodes.values()}.values())

    def get_node(self, node_id: str) -> Optional[ClusterNode]:
        return self._nodes.get(node_id)

    def _normalize_url(self, raw_url: str) -> str:
        url = raw_url.strip().rstrip("/")
        if url.startswith("pull://"):
            return url
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"http://{url}"
        import urllib.parse
        parsed = urllib.parse.urlsplit(url)
        if not parsed.port:
            url = f"{url}:8001"
        return url

    def add_node(self, req: NodeRegistrationRequest) -> ClusterNode:
        clean_url = self._normalize_url(req.url)
        # Check for existing by URL
        for existing in self._nodes.values():
            if existing.url == clean_url:
                existing.enabled = req.enabled
                if req.name:
                    existing.name = req.name
                self._save_nodes()
                return existing

        node_id = f"node-{len(self._nodes) + 1}"
        name = req.name or f"Worker-{node_id}"
        node = ClusterNode(
            id=node_id,
            name=name,
            url=clean_url,
            mode=getattr(req, "mode", "push"),
            enabled=req.enabled,
        )
        self._nodes[node_id] = node
        self._save_nodes()
        self.ping_node(node)
        return node

    def record_heartbeat(self, req: WorkerHeartbeatRequest) -> ClusterNode:
        """Records a heartbeat from an outbound pull worker and updates presence."""
        now_str = datetime.now(timezone.utc).isoformat()
        node = self._nodes.get(req.node_id)
        if not node:
            # Check by hostname, name, or URL matching
            req_host = (req.hostname or "").lower()
            req_host_short = req_host.split(".")[0] if req_host else ""
            req_node_id = req.node_id.lower()
            req_node_short = req_node_id.split(".")[0]
            req_name = (req.name or "").lower()

            for existing in self._nodes.values():
                # 1. Match existing hostname
                if existing.hostname:
                    e_host = existing.hostname.lower()
                    if e_host in (req_host, req_host_short) or e_host.split(".")[0] in (req_host, req_host_short):
                        node = existing
                        break
                # 2. Match existing name or ID
                e_name = existing.name.lower()
                e_id = existing.id.lower()
                if e_id in (req_node_id, req_node_short) or e_name in (req_name, req_host_short, req_node_short):
                    node = existing
                    break
                # 3. Match URL if it contains the worker's hostname or node ID
                e_url = existing.url.lower()
                if (req_host_short and req_host_short in e_url) or (req_node_short and req_node_short in e_url):
                    node = existing
                    break

        if not node:
            name = req.name or req.hostname or req.node_id
            node = ClusterNode(
                id=req.node_id,
                name=name,
                url=f"pull://{req.node_id}",
                mode="pull",
                enabled=True,
            )
            self._nodes[req.node_id] = node

        node.mode = "pull"
        node.is_online = True
        node.status = req.status
        node.is_idle = req.is_idle
        node.cores = req.cores
        node.cpu_pct = req.cpu_pct
        node.idle_seconds = req.idle_seconds
        node.hostname = req.hostname or node.hostname
        node.os = req.os or node.os
        node.last_seen = now_str
        # Index under req.node_id as well if it was registered under another ID
        if node.id != req.node_id and req.node_id not in self._nodes:
            self._nodes[req.node_id] = node

        self._save_nodes()
        logger.info(
            f"Heartbeat received: '{node.name}' (id={node.id}, online={node.is_online}, "
            f"idle={node.is_idle}, status={node.status}, cpu={node.cpu_pct}%)"
        )
        return node

    def remove_node(self, node_id: str) -> bool:
        if node_id in self._nodes:
            del self._nodes[node_id]
            self._save_nodes()
            return True
        return False

    def ping_node(self, node: ClusterNode, timeout_sec: float = 3.0) -> ClusterNode:
        """Synchronously pings a worker node and updates its live status."""
        if not node.enabled:
            node.is_online = False
            node.is_idle = False
            node.status = "disabled"
            return node

        now = datetime.now(timezone.utc)
        now_str = now.isoformat()

        # Handle pull workers (presence checked via recent heartbeat timestamp)
        if node.mode == "pull":
            if node.last_seen:
                try:
                    last_dt = datetime.fromisoformat(node.last_seen.replace("Z", "+00:00"))
                    # Allow 45s heartbeat window to tolerate minor network jitter
                    if (now - last_dt).total_seconds() < 45.0:
                        node.is_online = True
                        node.is_idle = (node.status == "idle" and node.is_idle)
                        return node
                except Exception:
                    pass
            node.is_online = False
            node.is_idle = False
            node.status = "offline"
            return node

        target_url = self._normalize_url(node.url)
        try:
            with httpx.Client(timeout=timeout_sec, verify=False) as client:
                resp = client.get(f"{target_url}/status")
                if resp.status_code == 200:
                    data = resp.json()
                    node.url = target_url
                    node.is_online = True
                    node.status = data.get("status", "idle")
                    node.is_idle = (node.status == "idle")
                    node.cores = data.get("cores", node.cores)
                    node.cpu_pct = data.get("cpu_pct", 0.0)
                    node.idle_seconds = data.get("idle_seconds", 0.0)
                    node.hostname = data.get("hostname", node.hostname)
                    node.os = data.get("os", node.os)
                    node.last_seen = now_str
                    node.active_task = data.get("active_task")
                    return node
        except Exception:
            pass

        node.is_online = False
        node.is_idle = False
        node.status = "offline"
        return node

    def refresh_all(self) -> list[ClusterNode]:
        for node in self._nodes.values():
            self.ping_node(node)
        return self.list_nodes()

    def get_available_idle_nodes(self) -> list[ClusterNode]:
        """Returns enabled, online nodes that are currently idle and ready for tasks."""
        self.refresh_all()
        # Deduplicate in case an existing node was aliased under both node-1 and hostname
        unique_nodes = {n.id: n for n in self._nodes.values()}
        idle_nodes = [n for n in unique_nodes.values() if n.enabled and n.is_online and n.is_idle]

        # Log node status breakdown for full operational transparency
        node_states = ", ".join(
            f"{n.name} (id={n.id}, mode={n.mode}, online={n.is_online}, idle={n.is_idle}, status={n.status})"
            for n in unique_nodes.values()
        )
        logger.info(
            f"Cluster status: {len(idle_nodes)}/{len(unique_nodes)} node(s) idle and ready for compute. "
            f"[{node_states}]"
        )
        return idle_nodes

    def get_cluster_status(self) -> ClusterStatus:
        nodes = self.list_nodes()
        online = [n for n in nodes if n.is_online]
        idle = [n for n in online if n.is_idle]
        total_cores = sum(n.cores for n in online)

        return ClusterStatus(
            enabled=settings.CLUSTER_ENABLED,
            storage_mode=settings.CLUSTER_STORAGE_MODE,
            total_nodes=len(nodes),
            online_nodes=len(online),
            idle_nodes=len(idle),
            total_cores=total_cores,
            active_jobs=sum(1 for n in online if n.status == "busy"),
            nodes=nodes,
        )


_REGISTRY_INSTANCE: Optional[NodeRegistry] = None


def get_node_registry() -> NodeRegistry:
    global _REGISTRY_INSTANCE
    if _REGISTRY_INSTANCE is None:
        _REGISTRY_INSTANCE = NodeRegistry()
    return _REGISTRY_INSTANCE
