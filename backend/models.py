import uuid
import datetime as dt
from sqlalchemy import Column, String, DateTime, JSON, Integer, ForeignKey
from database import Base


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> dt.datetime:
    return dt.datetime.utcnow()


class Run(Base):
    __tablename__ = "runs"
    run_id = Column(String, primary_key=True, default=lambda: new_id("run"))
    idea_text = Column(String, nullable=False)
    status = Column(String, nullable=False, default="IDEA_SUBMITTED")
    owner = Column(String, nullable=True)
    error_summary = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    evidence_id = Column(String, primary_key=True, default=lambda: new_id("ev"))
    run_id = Column(String, ForeignKey("runs.run_id"), nullable=False)
    claim = Column(String, nullable=False)
    source_url = Column(String, nullable=True)
    source_title = Column(String, nullable=True)
    accessed_at = Column(DateTime, default=utcnow)
    excerpt = Column(String, nullable=True)
    # "fact" (has a real source_url) or "inference" (the agent's own reasoning)
    evidence_type = Column(String, nullable=False)
    confidence = Column(String, nullable=False, default="medium")  # low/medium/high
    is_mock = Column(Integer, default=1)  # 1 = fixture/mock data, 0 = real adapter call


class ExperimentSpec(Base):
    __tablename__ = "experiment_specs"
    experiment_id = Column(String, primary_key=True, default=lambda: new_id("exp"))
    run_id = Column(String, ForeignKey("runs.run_id"), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    audience = Column(String, nullable=False)
    riskiest_assumption = Column(String, nullable=False)
    hypothesis = Column(String, nullable=False)
    offer = Column(String, nullable=False)
    cta = Column(String, nullable=False)
    primary_metric = Column(String, nullable=False)
    success_threshold = Column(String, nullable=False)
    stop_rule = Column(String, nullable=True)
    duration = Column(String, nullable=True)
    risks = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    approval_id = Column(String, primary_key=True, default=lambda: new_id("appr"))
    run_id = Column(String, ForeignKey("runs.run_id"), nullable=False)
    experiment_id = Column(String, nullable=False)
    experiment_version = Column(Integer, nullable=False)
    action_scope = Column(String, nullable=False)  # e.g. "publish_landing_page"
    approved_by = Column(String, nullable=False)
    decision = Column(String, nullable=False)  # "approved" / "rejected"
    content_hash = Column(String, nullable=False)  # hash of the exact approved content
    created_at = Column(DateTime, default=utcnow)


class ActionReceipt(Base):
    __tablename__ = "action_receipts"
    action_id = Column(String, primary_key=True, default=lambda: new_id("act"))
    run_id = Column(String, ForeignKey("runs.run_id"), nullable=False)
    action_type = Column(String, nullable=False)
    target = Column(String, nullable=True)
    status = Column(String, nullable=False)  # "success" / "blocked" / "failed"
    result_summary = Column(String, nullable=True)
    approval_id = Column(String, nullable=True)
    idempotency_key = Column(String, nullable=True, unique=True)
    created_at = Column(DateTime, default=utcnow)


class Event(Base):
    __tablename__ = "events"
    event_id = Column(String, primary_key=True, default=lambda: new_id("evt"))
    run_id = Column(String, ForeignKey("runs.run_id"), nullable=False)
    experiment_version = Column(Integer, nullable=False)
    event_type = Column(String, nullable=False)  # page_view / cta_click / lead_submitted
    # THE critical field: qa | demo | participant. Never let qa/demo count as real demand.
    source_class = Column(String, nullable=False)
    event_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class DecisionMemo(Base):
    __tablename__ = "decision_memos"
    memo_id = Column(String, primary_key=True, default=lambda: new_id("memo"))
    run_id = Column(String, ForeignKey("runs.run_id"), nullable=False)
    recommendation = Column(String, nullable=False)  # proceed/pivot/stop/inconclusive
    observations = Column(JSON, nullable=False)
    unknowns = Column(JSON, nullable=False)
    confidence = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow)
