"""
Masky-facing adapter: POST /api/runs starts a validation run, GET /api/runs/{id}
retrieves it. Both wrap the existing LangGraph `launchproof` package (the one
path in this repo that already runs research -> experiment -> asset -> report
automatically, pausing only at founder-approval gates) -- see
launchproof_routes.py's /graph-runs/* for the same Runner used directly.

Honesty constraint this module exists to enforce: this graph always runs in
demo mode here (deterministic fixtures, no real network/model calls, no real
landing-page traffic), so its ValidationReport is permanently simulated
(schemas.py: evidence_status="insufficient", demand_established=False,
real_customer_responses=0). GET responses below never claim real_participant_activity
or invent visitor/signup/conversion numbers -- metrics is null unless a real
source for them exists, which it does not in this implementation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from database import SessionLocal
from launchproof.runner import Runner
from launchproof.schemas import Idea, Settings
from masky_adapter import get_masky_provider, log_event, verify_webhook_signature

logger = logging.getLogger("launchproof.api_runs")
router = APIRouter(prefix="/api/runs", tags=["masky-adapter"])
webhook_router = APIRouter(tags=["masky-adapter"])

BASE_DATA_DIR = __import__("pathlib").Path(__file__).resolve().parent / "launchproof_data"

_runners: dict[str, Runner] = {}


def _get_runner(mode: str) -> Runner:
    if mode not in _runners:
        _runners[mode] = Runner(BASE_DATA_DIR / mode, mode)
    return _runners[mode]


# ---------- request/response schemas ----------

class StartRunRequest(BaseModel):
    idea_text: str = Field(min_length=10, max_length=3000)
    target_customer: str | None = Field(default=None, max_length=1000)
    requested_action: str | None = Field(default=None, max_length=1000)


# ---------- idea mapping ----------

def _idea_from_request(payload: StartRunRequest) -> Idea:
    """Masky's request carries idea_text/target_customer/requested_action;
    launchproof.schemas.Idea wants name/problem/customer/solution separately.
    There is no distinct 'solution' in Masky's payload, so problem and
    solution both default to idea_text verbatim -- a field-mapping choice,
    not an invented fact. customer defaults to a plain placeholder only if
    target_customer is missing, never to a guessed demographic."""
    return Idea(
        name=payload.idea_text[:100],
        problem=payload.idea_text,
        customer=payload.target_customer or "Not specified by founder",
        solution=payload.idea_text,
    )


# ---------- background execution ----------

def _run_graph_in_background(run_id: str, idea: Idea, mode: str) -> None:
    db: Session = SessionLocal()
    try:
        log_event(run_id=run_id, action="workflow_start", status="success", provider="n/a")
        runner = _get_runner(mode)
        runner.start(idea, Settings(mode=mode), run_id)
        log_event(run_id=run_id, action="agent_completion", status="success", provider="n/a")
        _maybe_notify_masky(run_id, mode)
    except Exception as e:  # noqa: BLE001 -- must persist the failure, never raise in a background task
        logger.exception("background graph run failed for %s", run_id)
        record = db.query(models.MaskyRun).filter(models.MaskyRun.run_id == run_id).first()
        if record:
            record.error = str(e)
            db.commit()
        log_event(run_id=run_id, action="workflow_failure", status="failure", provider="n/a", error=str(e))
    finally:
        db.close()


def _maybe_notify_masky(run_id: str, mode: str) -> None:
    """Best-effort only: a run is complete regardless of whether Avery was
    successfully notified. Only fires for a run that actually reached a
    terminal state (decision_ready / inconclusive)."""
    result = _run_report_snapshot(run_id, mode)
    if result["status"] in ("decision_ready", "inconclusive"):
        provider = get_masky_provider()
        provider.notify_result(run_id, result)
        log_event(run_id=run_id, action="final_recommendation", status="success", provider=provider.name)


# ---------- status translation ----------

_GATE_TO_STATUS = {
    "research": "researched",
    "experiment": "experiment_proposed",
    "asset": "asset_built",
    "report": "approval_required",
}

_RECOMMENDATION_MAP = {
    "proceed_to_real_test": "PROCEED",
    "pivot": "PIVOT",
    "stop": "STOP",
}

# Statuses your integration brief lists that this LangGraph-backed
# implementation cannot honestly produce, because the graph has no QA,
# publish, or real-traffic-measurement steps (see backend/main.py's
# state_machine.py for the *other*, not-yet-automated workflow that models
# those). Never fabricated here -- see the architecture note in this repo's
# README "Masky integration" section.
_UNSUPPORTED_BY_THIS_IMPLEMENTATION = {"qa_running", "qa_passed", "publishing", "published", "measuring"}


def _translate(graph_result: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {"metrics": None, "evidence_type": None, "limitations": [], "next_test": None,
            "recommendation": None, "summary": None}
    status_raw = graph_result.get("status")

    if status_raw is None and not graph_result.get("next_nodes"):
        # No checkpoint yet (or an empty one) -- the background task hasn't
        # produced a first durable state. Honest default: still researching.
        return {**base, "status": "researching"}

    if status_raw == "waiting_for_founder":
        gate = (graph_result.get("interrupt") or {}).get("review", {}).get("gate")
        return {**base, "status": _GATE_TO_STATUS.get(gate, "approval_required"), "gate": gate}

    if status_raw == "completed":
        report = (state or {}).get("report")
        if not report:
            # completed without a report dict shouldn't happen, but never
            # fabricate a recommendation if it does.
            return {**base, "status": "inconclusive",
                    "summary": "Run completed but no validation report was recorded.",
                    "limitations": ["No structured report available for this run."]}
        recommendation = _RECOMMENDATION_MAP.get(report.get("recommendation"))
        return {
            **base,
            "status": "decision_ready" if recommendation == "PROCEED" else "inconclusive",
            "recommendation": recommendation,
            "summary": report.get("synthesis"),
            "evidence_type": "insufficient_simulated_demo_evidence",
            "limitations": list(report.get("limitations", [])) + [
                "This run used LaunchProof's demo-mode simulated pipeline: no real "
                "participant traffic was measured. demand_established=False, "
                "real_customer_responses=0 by construction of this evidence_status."
            ],
            "next_test": (report.get("next_steps") or [None])[0],
        }

    if status_raw in ("cancelled", "revision_limit_reached"):
        reason = ("Founder rejected the idea; validation stopped before completion."
                  if status_raw == "cancelled" else
                  "Revision limit reached at a gate without reaching a decision.")
        return {**base, "status": "inconclusive", "summary": reason,
                "limitations": ["No conclusive evidence was gathered before the run stopped."]}

    return {**base, "status": "researching"}


def _run_report_snapshot(run_id: str, mode: str) -> dict[str, Any]:
    runner = _get_runner(mode)
    graph_result = runner.status(run_id)
    state = runner.state(run_id) if graph_result.get("status") == "completed" else None
    translated = _translate(graph_result, state)
    return {"run_id": run_id, **translated}


# ---------- routes ----------

@router.post("")
def start_run(payload: StartRunRequest, background_tasks: BackgroundTasks,
              idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    db = SessionLocal()
    try:
        if idempotency_key:
            existing = (db.query(models.MaskyRun)
                        .filter(models.MaskyRun.idempotency_key == idempotency_key)
                        .first())
            if existing:
                log_event(run_id=existing.run_id, action="run_creation", status="success",
                          provider="n/a", error=None)
                return {"run_id": existing.run_id,
                        "status": _run_report_snapshot(existing.run_id, existing.mode)["status"],
                        "message": "Idempotency key already used; returning the existing run"}

        idea = _idea_from_request(payload)  # validates idea_text via Idea.problem/solution constraints too
        mode = "demo"
        record = models.MaskyRun(idea_text=payload.idea_text, target_customer=payload.target_customer,
                                 requested_action=payload.requested_action, mode=mode,
                                 idempotency_key=idempotency_key)
        db.add(record)
        db.commit()
        db.refresh(record)

        log_event(run_id=record.run_id, action="run_creation", status="success", provider="n/a")
        background_tasks.add_task(_run_graph_in_background, record.run_id, idea, mode)

        return {"run_id": record.run_id, "status": "researching", "message": "Validation run started"}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        db.close()


@webhook_router.post("/webhooks/masky", include_in_schema=False)
async def masky_webhook(request: Request, x_masky_signature: str | None = Header(default=None)):
    """Inbound webhook receiver, in case Masky ever pushes events to this
    backend instead of (or in addition to) polling GET /api/runs/{id}.
    EXPERIMENTAL: Masky's actual webhook payload shape was never provided or
    verified, so this only authenticates the request and logs it -- it does
    not act on the payload. Do not route real run-state changes through this
    endpoint until Masky's real webhook contract is confirmed."""
    body = await request.body()
    if not verify_webhook_signature(body, x_masky_signature):
        raise HTTPException(status_code=401, detail="invalid or missing webhook signature")
    log_event(run_id="n/a", action="masky_webhook_received", status="success", provider="n/a")
    return {"received": True}


@router.get("/{run_id}")
def get_run(run_id: str):
    db = SessionLocal()
    try:
        record = db.query(models.MaskyRun).filter(models.MaskyRun.run_id == run_id).first()
        if not record:
            raise HTTPException(status_code=404, detail="run not found")
        if record.error:
            return {"run_id": run_id, "status": "failed", "recommendation": None,
                    "summary": None, "metrics": None, "evidence_type": None,
                    "limitations": [f"Workflow failed: {record.error}"], "next_test": None}
        return _run_report_snapshot(run_id, record.mode)
    finally:
        db.close()
