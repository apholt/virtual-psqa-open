from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class FieldSummary(BaseModel):
    beam_name: str
    gantry_angle: float
    energy_min_mev: float
    energy_max_mev: float
    number_of_layers: int
    total_spots: int
    total_mu: float


class PlanIngestionResponse(BaseModel):
    plan_id: int
    patient_id: str
    patient_name: str
    plan_label: str
    plan_name: str
    number_of_fields: int
    number_of_fractions: Optional[int] = None
    fields: list[FieldSummary]
    warnings: list[str]
    dicom_files_found: dict[str, int]


class PlanSummary(BaseModel):
    id: int
    patient_id: Optional[int] = None
    patient_identifier: Optional[str] = None
    patient_name: Optional[str] = None
    plan_label: str
    plan_name: str
    treatment_site: Optional[str] = None
    number_of_fractions: Optional[int] = None
    number_of_fields: int
    qa_status: str
    rtplan_uid: str
    created_at: datetime

    model_config = {"from_attributes": True}
