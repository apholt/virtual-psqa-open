from datetime import datetime
from typing import Optional
from pydantic import BaseModel
class GammaResultResponse(BaseModel):
    id: int
    plan_id: int
    fraction_number: Optional[int] = None
    field_name: str
    beam_number: Optional[int] = None
    comparison_type: str
    dd_percent: float
    dta_mm: float
    passing_rate: float
    threshold: float
    passed: bool
    gamma_map_path: Optional[str] = None
    created_at: datetime
    model_config = {"from_attributes": True}
