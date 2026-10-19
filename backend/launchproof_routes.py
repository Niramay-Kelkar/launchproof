"""
Scaffold integration of the LangGraph `launchproof` package (ported into
./launchproof/) as a parallel path alongside the existing status-machine
endpoints in main.py. Nothing here replaces /runs, /runs/{id}/research, etc.
-- it's a separate `/graph-runs` surface so the two can be compared/run
side by side before any real replacement happens.

Runner state lives in its own SQLite checkpointer under
./launchproof_data/<mode>/checkpoints.sqlite, keyed by run_id as the
LangGraph thread_id -- entirely separate from the SQLAlchemy models.py runs.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from launchproof.runner import Runner
from launchproof.schemas import FounderDecision, Idea, Settings

router = APIRouter(prefix="/graph-runs", tags=["launchproof-graph"])

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "launchproof_data"

_runners: dict[str, Runner] = {}


def get_runner(mode: str) -> Runner:
    if mode not in ("demo", "live"):
        raise HTTPException(status_code=422, detail="mode must be 'demo' or 'live'")
    if mode not in _runners:
        _runners[mode] = Runner(DATA_DIR / mode, mode)
    return _runners[mode]


class StartRequest(BaseModel):
    idea: Idea
    mode: str = "demo"
    settings: Settings | None = None
    run_id: str | None = None


class ResumeRequest(BaseModel):
    mode: str = "demo"
    decision: FounderDecision


@router.post("/start")
def start_graph_run(payload: StartRequest):
    runner = get_runner(payload.mode)
    settings = payload.settings or Settings(mode=payload.mode)
    return runner.start(payload.idea, settings, payload.run_id)


@router.post("/{run_id}/resume")
def resume_graph_run(run_id: str, payload: ResumeRequest):
    runner = get_runner(payload.mode)
    return runner.resume(run_id, payload.decision)


@router.get("/{run_id}/status")
def graph_run_status(run_id: str, mode: str = "demo"):
    runner = get_runner(mode)
    return runner.status(run_id)


@router.post("/{run_id}/retry")
def retry_graph_run(run_id: str, mode: str = "demo"):
    runner = get_runner(mode)
    return runner.retry_failed(run_id)
