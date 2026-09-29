from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class ClusterNode(BaseModel):
    id: str
    name: str
    url: str
    enabled: bool = True
    is_online: bool = False
    is_idle: bool = False
    status: str = "offline"  # "idle", "busy", "user_active", "offline", "unreachable"
    cores: int = 1
    cpu_pct: float = 0.0
    idle_seconds: float = 0.0
    hostname: Optional[str] = None
    os: Optional[str] = None
    last_seen: Optional[str] = None
    active_task: Optional[str] = None


class NodeRegistrationRequest(BaseModel):
    name: Optional[str] = None
    url: str
    enabled: bool = True


class BeamTaskRequest(BaseModel):
    job_id: int
    plan_id: int
    beam_no: int
    field_index: int
    total_fields: int
    plan_pencil_text: str
    delivered_protons: float
    primaries: int = 1_000_000
    uncertainty: float = 1.5
    dose_scaling: float = 0.9
    rbe: float = 1.10
    bdl_name: str = "auto"
    scanner: str = "default"
    ct_hash: Optional[str] = None


class BeamTaskResult(BaseModel):
    success: bool
    beam_no: int
    max_dose: float = 0.0
    dose_shape: list[int] = Field(default_factory=list)
    duration_seconds: float = 0.0
    error: Optional[str] = None


class ClusterStatus(BaseModel):
    enabled: bool
    storage_mode: str
    total_nodes: int
    online_nodes: int
    idle_nodes: int
    total_cores: int
    active_jobs: int
    nodes: list[ClusterNode]
