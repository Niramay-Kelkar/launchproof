"""LangGraph assembly: parallel agents, deterministic routers and durable human gates."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from . import nodes
from .providers import Services, TransientProviderError
from .schemas import LaunchState

TRANSIENT = RetryPolicy(max_attempts=3, initial_interval=1.0, retry_on=TransientProviderError)
RESEARCH = ["market_research", "competitor_scraper"]
BUILD = ["landing_page_generator", "synthetic_interviews"]


def route_intake(state):
    return RESEARCH if state.get("route") == "research" else "stop"


def route_research_quality(state):
    round_ids = set(state["market"]["source_ids"] + state["competitors"]["source_ids"])
    public = [e for e in state["evidence"] if e["id"] in round_ids and e["origin"] == "public_web"]
    if state["settings"]["mode"] == "live" and not public and state.get("research_auto_retries", 0) < 1:
        return "research_retry"
    return "research_gate"


def route_research(state):
    return {"experiment_designer": "experiment_designer", "research": RESEARCH}.get(
        state.get("route"), "stop")


def route_experiment(state):
    return {"build": BUILD, "experiment_designer": "experiment_designer"}.get(state.get("route"), "stop")


def route_asset(state):
    return {"materialize_asset": "materialize_asset", "build": BUILD}.get(state.get("route"), "stop")


def route_report(state):
    return {"finalize": "finalize", "build": BUILD}.get(state.get("route"), "stop")


def build_graph(services: Services, checkpointer):
    g = StateGraph(LaunchState)
    g.add_node("intake", nodes.intake)
    g.add_node("market_research", nodes.market_research_agent(services), retry_policy=TRANSIENT)
    g.add_node("competitor_scraper", nodes.competitor_scraper_agent(services), retry_policy=TRANSIENT)
    g.add_node("research_check", nodes.research_check)
    g.add_node("research_retry", nodes.research_retry)
    g.add_node("research_gate", nodes.approval_gate("research", "experiment_designer", "research"))
    g.add_node("experiment_designer", nodes.experiment_designer(services), retry_policy=TRANSIENT)
    g.add_node("experiment_gate", nodes.approval_gate("experiment", "build", "experiment_designer"))
    g.add_node("landing_page_generator", nodes.landing_page_generator_agent(services), retry_policy=TRANSIENT)
    g.add_node("synthetic_interviews", nodes.synthetic_customer_interview_agent(services), retry_policy=TRANSIENT)
    g.add_node("synthesize", nodes.synthesize)
    g.add_node("asset_gate", nodes.approval_gate("asset", "materialize_asset", "build"))
    g.add_node("materialize_asset", nodes.materialize_asset(services))
    g.add_node("report_gate", nodes.approval_gate("report", "finalize", "build"))
    g.add_node("finalize", nodes.finalize(services))
    g.add_node("stop", nodes.stop)

    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", route_intake, RESEARCH + ["stop"])
    g.add_edge(RESEARCH, "research_check")  # barrier: wait for both research branches
    g.add_conditional_edges("research_check", route_research_quality, ["research_retry", "research_gate"])
    g.add_edge("research_retry", "market_research")
    g.add_edge("research_retry", "competitor_scraper")
    g.add_conditional_edges("research_gate", route_research,
                            RESEARCH + ["experiment_designer", "stop"])
    g.add_edge("experiment_designer", "experiment_gate")
    g.add_conditional_edges("experiment_gate", route_experiment,
                            BUILD + ["experiment_designer", "stop"])
    g.add_edge(BUILD, "synthesize")  # barrier: asset and simulated interviews
    g.add_edge("synthesize", "asset_gate")
    g.add_conditional_edges("asset_gate", route_asset, ["materialize_asset"] + BUILD + ["stop"])
    g.add_edge("materialize_asset", "report_gate")
    g.add_conditional_edges("report_gate", route_report, ["finalize"] + BUILD + ["stop"])
    g.add_edge("finalize", END)
    g.add_edge("stop", END)
    return g.compile(checkpointer=checkpointer)


@contextmanager
def open_checkpointer(path: str | Path) -> Iterator[SqliteSaver]:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(path)) as saver:
        yield saver
