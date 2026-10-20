"""Live-mode graph paths with fake search/web/model objects; no network or keys."""
from pathlib import Path

from launchproof.network import FetchError, Page
from launchproof.providers import DemoModel, Services, UsageLedger
from launchproof.runner import Runner
from launchproof.schemas import FounderDecision, Idea, Settings

IDEA = Idea.model_validate_json((Path(__file__).parents[1] / "examples/idea.json").read_text())


class FlakySearch:
    def __init__(self):
        self.queries = []

    def search(self, query, count):
        self.queries.append(query)
        if len(self.queries) <= 2:  # both first-round branches find nothing
            return []
        return [{"title": "Real-looking", "url": "https://vendor.test/a", "content": ""},
                {"title": "Blocked", "url": "https://blocked.test/b", "content": ""}]


class FakeWeb:
    def fetch(self, url):
        if "blocked" in url:
            raise FetchError("robots.txt disallows this page")
        return Page(url, "Vendor page", "Vendor advertises task intake.")


def live_runner(tmp_path):
    runner = Runner(tmp_path, "demo")
    search = FlakySearch()
    runner.services = Services(DemoModel(), search, FakeWeb(), UsageLedger(tmp_path / "u.sqlite"),
                               tmp_path / "artifacts")
    return runner, search


def test_empty_live_research_retries_once_with_a_different_query_then_asks_founder(tmp_path):
    runner, search = live_runner(tmp_path)
    result = runner.start(IDEA, Settings(mode="live"), "live-1")
    assert result["status"] == "waiting_for_founder"
    state = runner.state("live-1")
    assert state["research_auto_retries"] == 1 and state["research_round"] == 1
    assert len({q for q in search.queries}) >= 2
    kinds = {(e["kind"], e["origin"]) for e in state["evidence"]}
    assert ("public_claim", "public_web") in kinds
    payload = result["interrupt"]["review"]["payload"]
    assert any("robots.txt" in e["reason"] for e in payload["fetch_errors"])


def test_public_claims_are_not_marked_verified_or_customer_evidence(tmp_path):
    runner, _ = live_runner(tmp_path)
    runner.start(IDEA, Settings(mode="live"), "live-2")
    for row in runner.state("live-2")["evidence"]:
        assert row["verified"] is False
        assert row["kind"] in {"public_claim", "assumption", "simulation", "operational"}
