from typing import Optional
from pydantic import BaseModel


class DoseSourceMeta(BaseModel):
    source: str                # tps | mcSquare | log
    n_planes: int
    rows: int
    cols: int
    spacing: list[float]       # [sz, sy, sx] mm
    max_dose: float


class PlanDoseInfo(BaseModel):
    plan_id: int
    verdict: str               # plan.qa_status
    sources: list[DoseSourceMeta]
    available_comparisons: list[str]
    default_plane: int         # max-dose plane of TPS (or 0)


class VerdictResponse(BaseModel):
    plan_id: int
    verdict: str
    note: Optional[str] = None
