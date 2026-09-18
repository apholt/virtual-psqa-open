from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class QAJobResponse(BaseModel):
    id: int
    plan_id: int
    job_type: str
    status: str
    progress: float
    fraction_number: Optional[int] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_path: Optional[str] = None

    model_config = {"from_attributes": True}


class QAJobCreate(BaseModel):
    plan_id: int
    job_type: str
    fraction_number: Optional[int] = None
