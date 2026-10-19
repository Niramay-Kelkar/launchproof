from pydantic import BaseModel
from typing import Optional, List


class RunCreate(BaseModel):
    idea_text: str
    owner: Optional[str] = None


class ExperimentSpecCreate(BaseModel):
    audience: str
    riskiest_assumption: str
    hypothesis: str
    offer: str
    cta: str
    primary_metric: str
    success_threshold: str
    stop_rule: Optional[str] = None
    duration: Optional[str] = None
    risks: Optional[str] = None


class ApprovalCreate(BaseModel):
    experiment_id: str
    experiment_version: int
    action_scope: str
    approved_by: str
    decision: str  # "approved" | "rejected"
    content_hash: str


class EventCreate(BaseModel):
    experiment_version: int
    event_type: str  # page_view | cta_click | lead_submitted
    source_class: str  # qa | demo | participant
    event_metadata: Optional[dict] = None
