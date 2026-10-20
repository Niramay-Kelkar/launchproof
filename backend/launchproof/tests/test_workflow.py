import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from launchproof.network import FetchError, public_target
from launchproof.nodes import materialize_asset
from launchproof.providers import BudgetExceeded, UsageLedger
from launchproof.runner import Runner
from launchproof.schemas import ExperimentPlan, FounderDecision, Idea, Settings, digest

IDEA = Idea.model_validate_json((Path(__file__).parents[1] / "examples/idea.json").read_text())
ORDER = ["research", "experiment", "asset", "report"]


def decide(result, choice="approve", feedback="", **extra):
    p = result["interrupt"]
    return FounderDecision(gate=p["review"]["gate"], review_hash=p["review_hash"],
                           decision=choice, feedback=feedback, **extra)


@pytest.fixture
def runner(tmp_path):
    return Runner(tmp_path, "demo")


def test_full_run_pauses_at_all_four_gates_and_writes_only_after_approval(runner, tmp_path):
    result = runner.start(IDEA, Settings(), "run-1")
    seen = []
    while result["status"] == "waiting_for_founder":
        gate = result["interrupt"]["review"]["gate"]
        seen.append(gate)
        if gate in ("research", "experiment", "asset"):
            assert not (tmp_path / "artifacts").exists(), "no files before asset approval"
        result = runner.resume("run-1", decide(result))
    assert seen == ORDER
    assert result["status"] == "completed"
    exported = Path(result["materialized"]["path"])
    assert (exported / "index.html").exists() and result["materialized"]["publication"] == "none"
    state = runner.state("run-1")
    assert state["report"]["demand_established"] is False
    assert state["report"]["real_customer_responses"] == 0
    assert any(e["kind"] == "simulation" for e in state["evidence"])
    assert all(e["kind"] != "customer_response" for e in state["evidence"])
    html = exported.joinpath("index.html").read_text()
    assert "not yet available" in html and "<form" not in html and "<script" not in html


def test_resume_survives_new_runner_instance_and_process_boundary(tmp_path):
    first = Runner(tmp_path, "demo").start(IDEA, Settings(), "run-2")
    restarted = Runner(tmp_path, "demo")  # new graph/checkpointer connection
    assert restarted.status("run-2")["status"] == "waiting_for_founder"
    after = restarted.resume("run-2", decide(first))
    assert after["interrupt"]["review"]["gate"] == "experiment"


def test_stale_or_wrong_gate_approval_is_rejected_and_run_stays_paused(runner):
    result = runner.start(IDEA, Settings(), "run-3")
    stale = decide(result).model_copy(update={"review_hash": "0" * 64})
    with pytest.raises(ValueError, match="Stale"):
        runner.resume("run-3", stale)
    wrong_gate = decide(result).model_copy(update={"gate": "asset"})
    with pytest.raises(ValueError, match="Stale"):
        runner.resume("run-3", wrong_gate)
    assert runner.status("run-3")["status"] == "waiting_for_founder"


def test_revision_reruns_branch_once_and_uses_feedback(runner):
    result = runner.start(IDEA, Settings(), "run-4")
    old_hash = result["interrupt"]["review_hash"]
    result = runner.resume("run-4", decide(result, "revise", "Focus on small landlords"))
    assert result["interrupt"]["review"]["gate"] == "research"
    assert result["interrupt"]["review"]["revision"] == 1
    assert result["interrupt"]["review_hash"] != old_hash
    state = runner.state("run-4")
    assert state["feedback"]["research"] == "Focus on small landlords"
    assert state["research_round"] == 1


def test_revision_limit_stops_instead_of_looping(runner):
    result = runner.start(IDEA, Settings(max_revisions_per_gate=1), "run-5")
    result = runner.resume("run-5", decide(result, "revise", "once"))
    result = runner.resume("run-5", decide(result, "revise", "twice"))
    assert result["status"] == "revision_limit_reached"


def test_reject_cancels_without_downstream_work(runner, tmp_path):
    result = runner.start(IDEA, Settings(), "run-6")
    result = runner.resume("run-6", decide(result, "reject"))
    assert result["status"] == "cancelled"
    assert "plan" not in runner.state("run-6")
    assert not (tmp_path / "artifacts").exists()


def test_revising_asset_requires_a_new_asset_approval_before_export(runner, tmp_path):
    result = runner.start(IDEA, Settings(), "run-7")
    result = runner.resume("run-7", decide(result))
    result = runner.resume("run-7", decide(result))  # approve experiment
    first_hash = result["interrupt"]["review_hash"]
    result = runner.resume("run-7", decide(result, "revise", "Make the claim more cautious"))
    assert result["interrupt"]["review"]["gate"] == "asset"
    assert result["interrupt"]["review"]["revision"] == 1
    assert runner.state("run-7")["asset_round"] == 1
    assert result["interrupt"]["review_hash"] != first_hash
    assert not (tmp_path / "artifacts").exists()


def test_revising_experiment_discards_experiment_and_downstream_approvals(runner):
    result = runner.start(IDEA, Settings(), "run-7b")
    result = runner.resume("run-7b", decide(result))
    result = runner.resume("run-7b", decide(result, "revise", "Use a longer observation window"))
    assert result["interrupt"]["review"]["gate"] == "experiment"
    approvals = runner.state("run-7b")["approvals"]
    assert set(approvals) == {"research"}


def test_asset_approval_is_bound_to_exact_content(runner):
    result = runner.start(IDEA, Settings(), "run-8")
    for _ in range(2):
        result = runner.resume("run-8", decide(result))
    review = result["interrupt"]["review"]["payload"]["bundle"]
    assert review["publication"] == "none" and review["data_collection"] == []
    assert review["content_sha256"] == hashlib.sha256(review["files"]["index.html"].encode()).hexdigest()


def test_materialize_node_refuses_missing_or_mismatched_approval(runner):
    result = runner.start(IDEA, Settings(), "run-9")
    for _ in range(2):
        result = runner.resume("run-9", decide(result))
    state = runner.state("run-9")
    node = materialize_asset(runner.services)
    with pytest.raises(ValidationError):
        node({**state, "approvals": {}})
    bad = {"gate": "asset", "review_hash": "f" * 64, "decision": "approve", "feedback": ""}
    with pytest.raises(ValueError, match="Stale"):
        node({**state, "approvals": {"asset": bad}})
    assert not (runner.services.output_dir).exists()


def test_revised_idea_replaces_idea_only_at_research_gate(runner):
    result = runner.start(IDEA, Settings(), "run-10")
    new = IDEA.model_copy(update={"customer": "Landlords with 5 to 20 rental units"})
    result = runner.resume("run-10", decide(result, "revise", "Narrow audience", revised_idea=new))
    assert runner.state("run-10")["idea"]["customer"] == new.customer
    with pytest.raises(ValidationError):
        FounderDecision(gate="asset", review_hash="0" * 64, decision="revise", feedback="x", revised_idea=new)


def test_experiment_schema_forbids_pii_spend_and_impossible_thresholds():
    base = dict(hypothesis="h", audience="a", primary_metric="qualified_discovery_requests",
                minimum_unique_visitors=30, minimum_requests=3, observation_hours=48,
                distribution="founder_shared_link", spend_cap_usd=0, data_collection=[],
                decision_rule="r", limitations=[])
    ExperimentPlan(**base)
    for bad in ({"data_collection": ["email"]}, {"spend_cap_usd": 25}, {"minimum_requests": 31}):
        with pytest.raises(ValidationError):
            ExperimentPlan(**{**base, **bad})


def test_usage_ledger_limits_persist_across_instances(tmp_path):
    UsageLedger(tmp_path / "u.sqlite").reserve("r", "model", 1)
    with pytest.raises(BudgetExceeded):
        UsageLedger(tmp_path / "u.sqlite").reserve("r", "model", 1)


@pytest.mark.parametrize("url", ["http://example.com", "https://127.0.0.1/", "https://[::1]/",
                                 "https://user:pw@example.com/", "https://example.com:8443/",
                                 "https://169.254.169.254/latest/meta-data"])
def test_network_policy_blocks_unsafe_targets(url):
    with pytest.raises(FetchError):
        public_target(url)
