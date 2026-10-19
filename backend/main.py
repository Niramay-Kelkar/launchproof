import os
import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

import models
import schemas
from database import Base, engine, get_db
from state_machine import transition, InvalidTransitionError
from research_adapter import run_research
from launchproof_routes import router as launchproof_router

BASE_DIR = Path(__file__).resolve().parent

Base.metadata.create_all(bind=engine)

app = FastAPI(title="LaunchProof API")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.include_router(launchproof_router)

# Same-origin serving (via the /app mount below) needs no CORS at all.
# This stays on only so the mockup can also be opened from a different
# port/file:// during development without fighting the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_run_or_404(db: Session, run_id: str) -> models.Run:
    run = db.query(models.Run).filter(models.Run.run_id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


def set_status(db: Session, run: models.Run, new_status: str):
    try:
        transition(run.status, new_status)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    run.status = new_status
    db.commit()
    db.refresh(run)


# ---------- health ----------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- runs ----------

@app.post("/runs")
def create_run(payload: schemas.RunCreate, db: Session = Depends(get_db)):
    run = models.Run(idea_text=payload.idea_text, owner=payload.owner, status="IDEA_SUBMITTED")
    db.add(run)
    db.commit()
    db.refresh(run)
    return {"run_id": run.run_id, "status": run.status}


@app.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)):
    run = get_run_or_404(db, run_id)
    return {
        "run_id": run.run_id,
        "idea_text": run.idea_text,
        "status": run.status,
        "error_summary": run.error_summary,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }


# ---------- research ----------

@app.post("/runs/{run_id}/research")
def do_research(run_id: str, db: Session = Depends(get_db)):
    run = get_run_or_404(db, run_id)
    set_status(db, run, "RESEARCHING")

    results = run_research(run.idea_text)
    if not results:
        set_status(db, run, "RESEARCH_INSUFFICIENT")
        raise HTTPException(status_code=422, detail="no evidence found for this idea")

    created = []
    for item in results:
        ev = models.EvidenceItem(
            run_id=run_id,
            claim=item["claim"],
            source_url=item.get("source_url"),
            source_title=item.get("source_title"),
            excerpt=item.get("excerpt"),
            evidence_type=item["evidence_type"],
            confidence=item.get("confidence", "medium"),
            is_mock=item.get("is_mock", 1),
        )
        db.add(ev)
        created.append(ev)
    db.commit()

    set_status(db, run, "RESEARCHED")
    return {"evidence_ids": [e.evidence_id for e in created]}


@app.get("/runs/{run_id}/evidence")
def get_evidence(run_id: str, db: Session = Depends(get_db)):
    get_run_or_404(db, run_id)
    items = db.query(models.EvidenceItem).filter(models.EvidenceItem.run_id == run_id).all()
    return [
        {
            "evidence_id": i.evidence_id,
            "claim": i.claim,
            "source_url": i.source_url,
            "source_title": i.source_title,
            "evidence_type": i.evidence_type,
            "confidence": i.confidence,
            "is_mock": bool(i.is_mock),
        }
        for i in items
    ]


# ---------- experiment spec ----------

@app.post("/runs/{run_id}/experiment")
def create_experiment(run_id: str, payload: schemas.ExperimentSpecCreate, db: Session = Depends(get_db)):
    run = get_run_or_404(db, run_id)
    if run.status != "RESEARCHED":
        raise HTTPException(status_code=409, detail=f"cannot propose an experiment from status {run.status}")

    existing = db.query(models.ExperimentSpec).filter(models.ExperimentSpec.run_id == run_id).count()
    spec = models.ExperimentSpec(run_id=run_id, version=existing + 1, **payload.model_dump())
    db.add(spec)
    db.commit()
    db.refresh(spec)

    set_status(db, run, "EXPERIMENT_PROPOSED")
    set_status(db, run, "APPROVAL_REQUIRED")
    return {"experiment_id": spec.experiment_id, "version": spec.version}


@app.get("/runs/{run_id}/experiment")
def get_latest_experiment(run_id: str, db: Session = Depends(get_db)):
    get_run_or_404(db, run_id)
    spec = (
        db.query(models.ExperimentSpec)
        .filter(models.ExperimentSpec.run_id == run_id)
        .order_by(models.ExperimentSpec.version.desc())
        .first()
    )
    if not spec:
        raise HTTPException(status_code=404, detail="no experiment spec yet")
    return {
        "experiment_id": spec.experiment_id,
        "version": spec.version,
        "audience": spec.audience,
        "riskiest_assumption": spec.riskiest_assumption,
        "hypothesis": spec.hypothesis,
        "offer": spec.offer,
        "cta": spec.cta,
        "primary_metric": spec.primary_metric,
        "success_threshold": spec.success_threshold,
        "stop_rule": spec.stop_rule,
        "duration": spec.duration,
        "risks": spec.risks,
    }


# ---------- approval gateway ----------

def content_hash_for(spec: models.ExperimentSpec) -> str:
    payload = {
        "experiment_id": spec.experiment_id,
        "version": spec.version,
        "offer": spec.offer,
        "cta": spec.cta,
        "hypothesis": spec.hypothesis,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@app.post("/runs/{run_id}/approve")
def approve(run_id: str, payload: schemas.ApprovalCreate, db: Session = Depends(get_db)):
    run = get_run_or_404(db, run_id)
    if run.status != "APPROVAL_REQUIRED":
        raise HTTPException(status_code=409, detail=f"no approval pending (status={run.status})")

    spec = db.query(models.ExperimentSpec).filter(
        models.ExperimentSpec.experiment_id == payload.experiment_id
    ).first()
    if not spec:
        raise HTTPException(status_code=404, detail="experiment not found")

    # Hard gate: the approval must match the CURRENT version and its exact content hash.
    if spec.version != payload.experiment_version:
        raise HTTPException(status_code=409, detail="approval references a stale experiment version, rejected")
    expected_hash = content_hash_for(spec)
    if expected_hash != payload.content_hash:
        raise HTTPException(status_code=409, detail="content hash mismatch: page/spec changed since approval was drafted")

    appr = models.Approval(
        run_id=run_id,
        experiment_id=payload.experiment_id,
        experiment_version=payload.experiment_version,
        action_scope=payload.action_scope,
        approved_by=payload.approved_by,
        decision=payload.decision,
        content_hash=payload.content_hash,
    )
    db.add(appr)
    db.commit()
    db.refresh(appr)

    if payload.decision == "approved":
        set_status(db, run, "ASSET_BUILT")
    else:
        set_status(db, run, "APPROVAL_REJECTED")

    return {"approval_id": appr.approval_id, "decision": appr.decision}


@app.get("/runs/{run_id}/approval-expected-hash")
def get_expected_hash(run_id: str, experiment_id: str, db: Session = Depends(get_db)):
    """Helper for the frontend: compute the hash it must submit back unchanged."""
    spec = db.query(models.ExperimentSpec).filter(models.ExperimentSpec.experiment_id == experiment_id).first()
    if not spec:
        raise HTTPException(status_code=404, detail="experiment not found")
    return {"content_hash": content_hash_for(spec)}


# ---------- landing page ----------

@app.get("/runs/{run_id}/landing", response_class=HTMLResponse)
def landing_page(run_id: str, request: Request, mode: str = "live", db: Session = Depends(get_db)):
    """mode=preview: renders anytime an experiment spec exists, events fire as 'qa'.
    mode=live (default): only renders once approved, events fire as 'participant'.
    This is what keeps a pre-approval preview from silently generating fake demand."""
    run = get_run_or_404(db, run_id)
    live_statuses = {"ASSET_BUILT", "QA_RUNNING", "QA_PASSED", "PUBLISHING", "PUBLISHED", "MEASURING"}

    spec = (
        db.query(models.ExperimentSpec)
        .filter(models.ExperimentSpec.run_id == run_id)
        .order_by(models.ExperimentSpec.version.desc())
        .first()
    )
    if not spec:
        raise HTTPException(status_code=409, detail="no experiment spec yet")

    if mode == "preview":
        event_source_class = "qa"
    elif mode == "live":
        if run.status not in live_statuses:
            raise HTTPException(status_code=409, detail="no approved page to render yet")
        event_source_class = "participant"
    else:
        raise HTTPException(status_code=422, detail="mode must be 'preview' or 'live'")

    return templates.TemplateResponse(
        "landing_page.html",
        {
            "request": request,
            "run_id": run_id,
            "experiment_version": spec.version,
            "offer": spec.offer,
            "hypothesis": spec.hypothesis,
            "cta": spec.cta,
            "mode": mode,
            "event_source_class": event_source_class,
        },
    )


# ---------- events ----------

@app.post("/runs/{run_id}/events")
def record_event(run_id: str, payload: schemas.EventCreate, db: Session = Depends(get_db)):
    get_run_or_404(db, run_id)
    if payload.source_class not in {"qa", "demo", "participant"}:
        raise HTTPException(status_code=422, detail="source_class must be qa, demo, or participant")

    ev = models.Event(
        run_id=run_id,
        experiment_version=payload.experiment_version,
        event_type=payload.event_type,
        source_class=payload.source_class,
        event_metadata=payload.event_metadata,
    )
    db.add(ev)
    db.commit()
    return {"event_id": ev.event_id}


@app.get("/runs/{run_id}/events")
def list_events(run_id: str, db: Session = Depends(get_db)):
    get_run_or_404(db, run_id)
    events = db.query(models.Event).filter(models.Event.run_id == run_id).all()
    return [
        {
            "event_id": e.event_id,
            "event_type": e.event_type,
            "source_class": e.source_class,
            "experiment_version": e.experiment_version,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


# ---------- report / proof pack ----------

@app.get("/runs/{run_id}/report")
def get_report(run_id: str, db: Session = Depends(get_db)):
    run = get_run_or_404(db, run_id)

    evidence = db.query(models.EvidenceItem).filter(models.EvidenceItem.run_id == run_id).all()
    events = db.query(models.Event).filter(models.Event.run_id == run_id).all()
    spec = (
        db.query(models.ExperimentSpec)
        .filter(models.ExperimentSpec.run_id == run_id)
        .order_by(models.ExperimentSpec.version.desc())
        .first()
    )
    approvals = db.query(models.Approval).filter(models.Approval.run_id == run_id).all()

    events_by_class = {"qa": 0, "demo": 0, "participant": 0}
    for e in events:
        events_by_class[e.source_class] = events_by_class.get(e.source_class, 0) + 1

    real_participant_events = events_by_class["participant"]

    # The rule that matters: only real participant events count as evidence of demand.
    if real_participant_events == 0:
        recommendation = "inconclusive"
        reason = "No real participant evidence was collected yet. QA/demo activity does not count as demand."
    elif spec and real_participant_events < 3:
        recommendation = "inconclusive"
        reason = f"Only {real_participant_events} real participant event(s) recorded — sample size too small to conclude."
    else:
        recommendation = "proceed"
        reason = f"{real_participant_events} real participant events recorded against the approved experiment."

    # Anything without a real source_url is, by construction, unsourced --
    # surface it as an explicit unknown rather than letting it blend in
    # with cited facts.
    learned = [e for e in evidence if e.source_url]
    unknowns = [e for e in evidence if not e.source_url]

    return {
        "run_id": run_id,
        "status": run.status,
        "evidence": [
            {
                "evidence_id": e.evidence_id,
                "claim": e.claim,
                "source_url": e.source_url,
                "source_title": e.source_title,
                "evidence_type": e.evidence_type,
                "is_mock": bool(e.is_mock),
            }
            for e in evidence
        ],
        "learned": [{"evidence_id": e.evidence_id, "claim": e.claim, "source_url": e.source_url} for e in learned],
        "unknowns": [{"evidence_id": e.evidence_id, "claim": e.claim} for e in unknowns],
        "experiment_version": spec.version if spec else None,
        "approvals": [{"approval_id": a.approval_id, "decision": a.decision} for a in approvals],
        "events_by_source_class": events_by_class,
        "recommendation": recommendation,
        "reason": reason,
    }


# ---------- static frontend ----------
# Serves the mockup as /app so fetch() calls to the API below are
# same-origin: no CORS configuration to fight during the demo.
app.mount("/app", StaticFiles(directory=str(BASE_DIR.parent / "frontend"), html=True), name="frontend")
