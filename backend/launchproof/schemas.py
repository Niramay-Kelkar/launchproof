"""Validated I/O models; checkpoint state contains JSON, never clients or secrets."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def merge_records(old: list[dict], new: list[dict]) -> list[dict]:
    """Idempotent ledger reducer for parallel branches and replay."""
    records = {record["id"]: record for record in old}
    for record in new:
        key = record["id"]
        if key in records and records[key] != record:
            raise ValueError(f"Conflicting immutable ledger record: {key}")
        records[key] = record
    return [records[key] for key in sorted(records)]


class Idea(Model):
    name: str = Field(min_length=1, max_length=100)
    problem: str = Field(min_length=10, max_length=3000)
    customer: str = Field(min_length=3, max_length=1000)
    solution: str = Field(min_length=10, max_length=3000)
    geography: str | None = None
    constraints: list[str] = Field(default_factory=list, max_length=20)
    competitor_urls: list[str] = Field(default_factory=list, max_length=6)


class Settings(Model):
    mode: Literal["demo", "live"] = "demo"
    max_revisions_per_gate: int = Field(default=2, ge=0, le=5)
    max_research_results: int = Field(default=4, ge=1, le=6)
    persona_count: int = Field(default=3, ge=1, le=6)
    max_model_calls: int = Field(default=50, ge=1, le=200)
    # A hard call reservation limit, not an estimate of provider billing.
    max_search_calls: int = Field(default=30, ge=1, le=100)


class Evidence(Model):
    id: str
    kind: Literal["public_claim", "assumption", "simulation", "operational"]
    origin: Literal["public_web", "model", "demo_fixture", "local_system"]
    title: str
    text: str
    url: str | None = None
    observed_at: str
    verified: bool = False
    limitations: list[str] = Field(default_factory=list)


class Claim(Model):
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["sourced_observation", "hypothesis"]


class MarketReport(Model):
    summary: str
    claims: list[Claim]
    riskiest_assumptions: list[str]
    unknowns: list[str]
    # No invented TAM/SAM/SOM fields. Add sourced estimates explicitly if needed.


class Competitor(Model):
    name: str
    url: str
    positioning: str
    advertised_features: list[str]
    pricing_observation: str
    evidence_ids: list[str]


class CompetitorReport(Model):
    competitors: list[Competitor]
    gaps_to_test: list[str]
    limitations: list[str]


class ExperimentPlan(Model):
    hypothesis: str
    audience: str
    primary_metric: Literal["qualified_discovery_requests"]
    minimum_unique_visitors: int = Field(ge=10, le=10000)
    minimum_requests: int = Field(ge=1, le=1000)
    observation_hours: int = Field(ge=1, le=720)
    distribution: Literal["founder_shared_link"]
    spend_cap_usd: Literal[0] = 0
    data_collection: list[str]
    decision_rule: str
    limitations: list[str]

    @model_validator(mode="after")
    def coherent_thresholds(self):
        if self.minimum_requests > self.minimum_unique_visitors:
            raise ValueError("Requests threshold cannot exceed planned visitor threshold")
        if self.data_collection:
            raise ValueError("This implementation generates previews only; no PII collection")
        return self


class LandingCopy(Model):
    headline: str = Field(min_length=5, max_length=160)
    subheadline: str = Field(min_length=10, max_length=500)
    benefits: list[str] = Field(min_length=2, max_length=4)
    cta_label: str = Field(min_length=3, max_length=60)
    availability: Literal["Research concept — not yet available"]


class Persona(Model):
    id: str
    role: str
    context: str
    skepticism: str


class Interview(Model):
    persona: Persona
    transcript: list[str] = Field(min_length=2, max_length=16)
    objections: list[str]
    proposed_test: str
    is_synthetic: Literal[True] = True
    # Deliberately no fake purchase intent / satisfaction scores.


class InterviewBatch(Model):
    interviews: list[Interview] = Field(min_length=1, max_length=6)
    limitations: str


class ValidationReport(Model):
    recommendation: Literal["proceed_to_real_test", "pivot", "stop"]
    evidence_status: Literal["insufficient"] = "insufficient"
    demand_established: Literal[False] = False
    real_customer_responses: Literal[0] = 0
    synthesis: str
    public_evidence_ids: list[str]
    simulated_objections: list[str]
    next_steps: list[str]
    limitations: list[str]


Gate = Literal["research", "experiment", "asset", "report"]
Decision = Literal["approve", "revise", "reject"]


class FounderDecision(Model):
    gate: Gate
    review_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Decision
    feedback: str = Field(default="", max_length=5000)
    revised_idea: Idea | None = None

    @model_validator(mode="after")
    def revision_is_explicit(self):
        if self.decision == "revise" and not self.feedback.strip():
            raise ValueError("Revision requires actionable feedback")
        if self.revised_idea is not None and not (
            self.gate == "research" and self.decision == "revise"
        ):
            raise ValueError("Only a research revision can replace the idea")
        return self


class LaunchState(TypedDict, total=False):
    schema_version: int
    run_id: str
    idea: dict
    settings: dict
    research_round: int
    research_auto_retries: int
    asset_round: int
    revisions: dict[str, int]
    feedback: dict[str, str]
    approvals: dict[str, dict]
    route: str
    status: str
    market: dict
    competitors: dict
    plan: dict
    landing: dict
    interviews: dict
    report: dict
    artifact_bundle: dict
    materialized: dict
    evidence: Annotated[list[dict], merge_records]
    audit: Annotated[list[dict], merge_records]
    errors: Annotated[list[dict], merge_records]
