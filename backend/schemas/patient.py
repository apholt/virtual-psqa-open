from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel


class PatientBase(BaseModel):
    patient_id: str
    patient_name: str
    date_of_birth: Optional[date] = None
    sex: Optional[str] = None


class PatientCreate(PatientBase):
    pass


class PatientResponse(PatientBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class PatientWithLatestPlan(BaseModel):
    id: int
    patient_id: str
    patient_name: str
    latest_plan_label: Optional[str] = None
    latest_plan_site: Optional[str] = None
    qa_status: str
    days_since_created: int
    number_of_fields: Optional[int] = None

    model_config = {"from_attributes": True}
