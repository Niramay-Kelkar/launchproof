"""
Tests for POST /api/runs and GET /api/runs/{run_id} -- the Masky-facing
adapter on top of the LangGraph `launchproof` package. Runs the graph for
real, in demo mode (deterministic fixtures, no network/model calls), through
a fresh TestClient/DB/checkpointer per test via isolated tmp dirs.
"""
import time

import pytest
from fastapi.testclient import TestClient

import api_runs
from launchproof.schemas import FounderDecision
from main import app


@pytest.fixture
def client():
    """Uses the real app, real backend/launchproof.db and real
    launchproof_data/ checkpointer dir -- same as the existing test files in
    this directory, and same as a developer hitting the API with curl. Each
    run gets a fresh random run_id, so tests never collide with each other
    or with pre-existing local dev data; nothing here needs per-test
    database/module isolation."""
    return TestClient(app)


def _approve_gate(client, run_id, decision="approve", feedback=""):
    status = api_runs._get_runner("demo").status(run_id)
    review = status["interrupt"]["review"]
    founder_decision = FounderDecision(gate=review["gate"], review_hash=status["interrupt"]["review_hash"],
                                        decision=decision, feedback=feedback)
    return api_runs._get_runner("demo").resume(run_id, founder_decision)


def _approve_all_gates(client, run_id, max_gates=6):
    for _ in range(max_gates):
        status = api_runs._get_runner("demo").status(run_id)
        if status.get("status") != "waiting_for_founder":
            return status
        _approve_gate(client, run_id)
    return api_runs._get_runner("demo").status(run_id)


def _wait_until_not_researching(client, run_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/runs/{run_id}").json()
        if body["status"] != "researching":
            return body
        time.sleep(0.05)
    return client.get(f"/api/runs/{run_id}").json()


VALID_PAYLOAD = {
    "idea_text": "An AI career assistant for international graduate students",
    "target_customer": "International graduate students",
    "requested_action": "Validate this startup idea",
}


# ---------- 1. valid POST ----------

def test_valid_post_starts_a_run(client):
    r = client.post("/api/runs", json=VALID_PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "researching"
    assert body["message"] == "Validation run started"
    assert body["run_id"]


# ---------- 2. missing idea_text ----------

def test_missing_idea_text_is_rejected(client):
    r = client.post("/api/runs", json={"target_customer": "x"})
    assert r.status_code == 422


# ---------- 3. invalid request body ----------

def test_invalid_request_body_is_rejected(client):
    r = client.post("/api/runs", json={"idea_text": 12345})
    assert r.status_code == 422


# ---------- 4. unique run_id generation ----------

def test_each_run_gets_a_unique_run_id(client):
    ids = {client.post("/api/runs", json=VALID_PAYLOAD).json()["run_id"] for _ in range(5)}
    assert len(ids) == 5


# ---------- 5. GET for a valid run ----------

def test_get_valid_run_returns_its_state(client):
    run_id = client.post("/api/runs", json=VALID_PAYLOAD).json()["run_id"]
    r = client.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    assert r.json()["run_id"] == run_id


# ---------- 6. GET for an invalid run_id ----------

def test_get_invalid_run_id_returns_404(client):
    r = client.get("/api/runs/does-not-exist")
    assert r.status_code == 404


# ---------- 7. intermediate workflow status ----------

def test_intermediate_status_reflects_pending_approval_gate(client):
    run_id = client.post("/api/runs", json=VALID_PAYLOAD).json()["run_id"]
    body = _wait_until_not_researching(client, run_id)
    assert body["status"] in ("researched", "experiment_proposed", "asset_built", "approval_required")
    assert body["recommendation"] is None


# ---------- 8. failed workflow ----------

def test_failed_workflow_is_reported_as_failed(client, monkeypatch):
    import api_runs

    def boom(run_id, idea, mode):
        import models
        from database import SessionLocal
        db = SessionLocal()
        record = db.query(models.MaskyRun).filter(models.MaskyRun.run_id == run_id).first()
        record.error = "simulated graph failure"
        db.commit()
        db.close()

    monkeypatch.setattr(api_runs, "_run_graph_in_background", boom)
    run_id = client.post("/api/runs", json=VALID_PAYLOAD).json()["run_id"]
    time.sleep(0.1)
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["status"] == "failed"
    assert "simulated graph failure" in body["limitations"][0]


# ---------- 9. inconclusive workflow ----------

def test_rejected_run_is_reported_as_inconclusive(client):
    run_id = client.post("/api/runs", json=VALID_PAYLOAD).json()["run_id"]
    _wait_until_not_researching(client, run_id)
    _approve_gate(client, run_id, decision="reject")

    body = client.get(f"/api/runs/{run_id}").json()
    assert body["status"] == "inconclusive"
    assert body["recommendation"] is None
    assert body["limitations"]


# ---------- 10. duplicate request / retry (idempotency) ----------

def test_duplicate_idempotency_key_returns_the_same_run(client):
    r1 = client.post("/api/runs", json=VALID_PAYLOAD, headers={"Idempotency-Key": "k-1"})
    r2 = client.post("/api/runs", json={**VALID_PAYLOAD, "idea_text": "A completely different idea here"},
                      headers={"Idempotency-Key": "k-1"})
    assert r1.json()["run_id"] == r2.json()["run_id"]


# ---------- 11. prevention of fabricated success ----------

def test_no_recommendation_or_metrics_before_completion(client):
    run_id = client.post("/api/runs", json=VALID_PAYLOAD).json()["run_id"]
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["recommendation"] is None
    assert body["metrics"] is None


def test_decision_ready_never_claims_real_participant_metrics(client):
    """This implementation always runs in demo mode -- it must never invent
    visitors/signups/conversion numbers it never measured."""
    run_id = client.post("/api/runs", json=VALID_PAYLOAD).json()["run_id"]
    _wait_until_not_researching(client, run_id)
    final = _approve_all_gates(client, run_id)
    assert final["status"] == "completed"
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["status"] in ("decision_ready", "inconclusive")
    assert body["metrics"] is None
    assert body["evidence_type"] != "real_participant_activity"
    assert body["limitations"]


# ---------- 12. fixture/demo events excluded from participant metrics ----------

def test_demo_mode_report_is_explicitly_labeled_simulated(client):
    run_id = client.post("/api/runs", json=VALID_PAYLOAD).json()["run_id"]
    _wait_until_not_researching(client, run_id)
    _approve_all_gates(client, run_id)
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["status"] == "decision_ready" or body["status"] == "inconclusive"
    if body["status"] == "decision_ready":
        assert any("demo-mode simulated" in note for note in body["limitations"])


# ---------- 13/14/15. Masky adapter: provider selection, success, and every
# documented-against-nothing failure mode (auth, malformed, timeout, http
# error), plus duplicate-notification protection. All against mocked httpx --
# no real network call to masky.ai is ever made here.

class _FakeResponse:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}

    def json(self):
        return self._json_body


def test_masky_adapter_mock_provider_used_without_credentials(monkeypatch):
    import masky_adapter
    monkeypatch.setattr(masky_adapter, "MASKY_API_KEY", "")
    monkeypatch.setattr(masky_adapter, "MASKY_AVATAR_ID", "")
    monkeypatch.setattr(masky_adapter, "MASKY_AVATAR_OWNER_USER_ID", "")
    provider = masky_adapter.get_masky_provider()
    assert isinstance(provider, masky_adapter.MockMaskyProvider)
    result = provider.notify_result("run-1", {"status": "decision_ready"})
    assert result["delivered"] is False
    assert result["provider"] == "mock"


def test_masky_adapter_mock_provider_used_when_avatar_owner_missing(monkeypatch):
    """avatarOwnerUserId is required by the documented /conversations
    contract -- having an API key and avatarId is not enough on its own."""
    import masky_adapter
    monkeypatch.setattr(masky_adapter, "MASKY_API_KEY", "key")
    monkeypatch.setattr(masky_adapter, "MASKY_AVATAR_ID", "avatar")
    monkeypatch.setattr(masky_adapter, "MASKY_AVATAR_OWNER_USER_ID", "")
    assert isinstance(masky_adapter.get_masky_provider(), masky_adapter.MockMaskyProvider)


def test_masky_adapter_real_provider_selected_with_full_credentials(monkeypatch):
    import masky_adapter
    monkeypatch.setattr(masky_adapter, "MASKY_API_KEY", "key")
    monkeypatch.setattr(masky_adapter, "MASKY_AVATAR_ID", "avatar")
    monkeypatch.setattr(masky_adapter, "MASKY_AVATAR_OWNER_USER_ID", "twitch:12345")
    provider = masky_adapter.get_masky_provider()
    assert isinstance(provider, masky_adapter.RealMaskyProvider)


def _provider():
    import masky_adapter
    return masky_adapter.RealMaskyProvider("key", "avatar", "twitch:12345", "https://masky.ai/api")


def test_real_provider_success_creates_conversation_and_sends_text_turn(monkeypatch):
    import masky_adapter
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        if url.endswith("/conversations"):
            return _FakeResponse(200, {"conversationId": "conv-1", "liveUrl": "https://masky.ai/live/c-1"})
        return _FakeResponse(200, {"status": "ready"})

    monkeypatch.setattr(masky_adapter.httpx, "post", fake_post)
    result = _provider().notify_result("run-1", {"status": "decision_ready", "recommendation": "PROCEED"})

    assert result == {"provider": "real_unverified", "status": "delivered", "delivered": True,
                       "conversation_id": "conv-1"}
    assert calls[0][0] == "https://masky.ai/api/conversations"
    assert calls[0][1] == {"avatarId": "avatar", "avatarOwnerUserId": "twitch:12345"}
    assert calls[1][0] == "https://masky.ai/api/conversations/conv-1/turn"
    assert calls[1][1]["mode"] == "speak"
    assert calls[1][1]["output"] == "text"
    # never audio/video, and headers never carry anything beyond the bearer token
    assert set(calls[1][1]) == {"userText", "mode", "output"}
    assert calls[0][2] == {"Authorization": "Bearer key"}


def test_real_provider_authentication_failure_at_conversation_step(monkeypatch):
    import masky_adapter
    monkeypatch.setattr(masky_adapter.httpx, "post", lambda *a, **k: _FakeResponse(401))
    result = _provider().notify_result("run-1", {"status": "decision_ready"})
    assert result == {"provider": "real_unverified", "status": "failed", "delivered": False,
                       "reason": "authentication_failed"}


def test_real_provider_malformed_conversation_response(monkeypatch):
    import masky_adapter
    monkeypatch.setattr(masky_adapter.httpx, "post", lambda *a, **k: _FakeResponse(200, {"unexpected": "shape"}))
    result = _provider().notify_result("run-1", {"status": "decision_ready"})
    assert result == {"provider": "real_unverified", "status": "failed", "delivered": False,
                       "reason": "malformed_conversation_response"}


def test_real_provider_timeout_at_conversation_creation_is_uncertain_not_failed(monkeypatch):
    """A timeout before we know whether the conversation was created must be
    reported as uncertain, not a confirmed failure -- and must not be
    retried (no second call happens here)."""
    import httpx
    import masky_adapter
    calls = []

    def raise_timeout(*a, **k):
        calls.append(1)
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(masky_adapter.httpx, "post", raise_timeout)
    result = _provider().notify_result("run-1", {"status": "decision_ready"})
    assert result["status"] == "uncertain"
    assert result["delivered"] is False
    assert len(calls) == 1  # exactly one attempt -- no retry


def test_real_provider_conversation_succeeds_but_turn_fails(monkeypatch):
    """The conversation was actually created -- its id must still be
    returned so a human can follow up, even though the turn itself failed."""
    import masky_adapter

    def fake_post(url, json=None, headers=None, timeout=None):
        if url.endswith("/conversations"):
            return _FakeResponse(200, {"conversationId": "conv-2"})
        return _FakeResponse(500)

    monkeypatch.setattr(masky_adapter.httpx, "post", fake_post)
    result = _provider().notify_result("run-1", {"status": "decision_ready"})
    assert result == {"provider": "real_unverified", "status": "failed", "delivered": False,
                       "conversation_id": "conv-2", "reason": "turn_http_500"}


def test_real_provider_turn_timeout_after_conversation_created_is_uncertain(monkeypatch):
    import httpx
    import masky_adapter

    def fake_post(url, json=None, headers=None, timeout=None):
        if url.endswith("/conversations"):
            return _FakeResponse(200, {"conversationId": "conv-3"})
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(masky_adapter.httpx, "post", fake_post)
    result = _provider().notify_result("run-1", {"status": "decision_ready"})
    assert result["status"] == "uncertain"
    assert result["conversation_id"] == "conv-3"


def test_real_provider_never_logs_api_key_or_auth_header(monkeypatch, caplog):
    import logging
    import masky_adapter
    monkeypatch.setattr(masky_adapter.httpx, "post", lambda *a, **k: _FakeResponse(401))
    provider = masky_adapter.RealMaskyProvider("mky_super_secret_token", "avatar",
                                               "twitch:12345", "https://masky.ai/api")
    with caplog.at_level(logging.INFO, logger="launchproof.masky_adapter"):
        provider.notify_result("run-1", {"status": "decision_ready"})
    for record in caplog.records:
        assert "mky_super_secret_token" not in record.getMessage()
        assert "Bearer" not in record.getMessage()


# ---------- duplicate-notification protection ----------

def test_notify_masky_is_attempted_at_most_once_per_run(monkeypatch):
    import api_runs
    import masky_adapter
    from database import SessionLocal

    calls = []

    class CountingMock(masky_adapter.MockMaskyProvider):
        def notify_result(self, run_id, result):
            calls.append(run_id)
            return super().notify_result(run_id, result)

    monkeypatch.setattr(api_runs, "get_masky_provider", lambda: CountingMock())

    db = SessionLocal()
    import models
    record = models.MaskyRun(idea_text="An idea long enough to pass validation here")
    db.add(record)
    db.commit()
    db.refresh(record)

    decision_ready = {"status": "decision_ready", "recommendation": "PROCEED"}
    api_runs._maybe_notify_masky(db, record, decision_ready)
    api_runs._maybe_notify_masky(db, record, decision_ready)  # simulates a second GET poll
    db.refresh(record)  # the claim/outcome is written via a Core UPDATE, not the ORM object directly
    status = record.masky_notify_status
    db.close()

    assert len(calls) == 1
    assert status == "mocked"


def test_non_terminal_run_never_invokes_the_provider(monkeypatch):
    """A run still waiting for a founder gate must never trigger a Masky
    call -- only decision_ready/inconclusive are terminal."""
    import api_runs
    import masky_adapter
    import models
    from database import SessionLocal

    calls = []

    class CountingMock(masky_adapter.MockMaskyProvider):
        def notify_result(self, run_id, result):
            calls.append(run_id)
            return super().notify_result(run_id, result)

    monkeypatch.setattr(api_runs, "get_masky_provider", lambda: CountingMock())

    db = SessionLocal()
    record = models.MaskyRun(idea_text="An idea long enough to pass validation here")
    db.add(record)
    db.commit()
    db.refresh(record)

    for status in ("researching", "researched", "experiment_proposed", "asset_built", "approval_required"):
        api_runs._maybe_notify_masky(db, record, {"status": status})
    db.close()

    assert calls == []


@pytest.mark.parametrize("prior_status", ["delivered", "failed", "uncertain", "mocked", "in_progress"])
def test_a_second_call_after_any_prior_outcome_never_invokes_the_provider(monkeypatch, prior_status):
    """Whatever the first attempt's outcome (success, confirmed failure, an
    ambiguous timeout, the mock path, or even a still-in-progress claim from
    a crashed attempt), a later call must never call the provider again --
    there is no automatic retry for any of these states."""
    import api_runs
    import masky_adapter
    import models
    from database import SessionLocal

    calls = []

    class CountingMock(masky_adapter.MockMaskyProvider):
        def notify_result(self, run_id, result):
            calls.append(run_id)
            return super().notify_result(run_id, result)

    monkeypatch.setattr(api_runs, "get_masky_provider", lambda: CountingMock())

    db = SessionLocal()
    record = models.MaskyRun(idea_text="An idea long enough to pass validation here",
                             masky_notify_status=prior_status)
    db.add(record)
    db.commit()
    db.refresh(record)

    api_runs._maybe_notify_masky(db, record, {"status": "decision_ready", "recommendation": "PROCEED"})
    db.close()

    assert calls == []


def test_provider_failure_and_uncertain_outcomes_are_recorded_without_retry(monkeypatch):
    """A confirmed failure and an ambiguous timeout must each be persisted
    as their own distinct masky_notify_status, and neither is retried by a
    second call."""
    import api_runs
    import models
    from database import SessionLocal

    for outcome_status in ("failed", "uncertain"):
        calls = []

        class OnceProvider:
            name = "real_unverified"

            def notify_result(self, run_id, result):
                calls.append(run_id)
                return {"provider": self.name, "status": outcome_status, "delivered": False,
                        "reason": "simulated"}

        monkeypatch.setattr(api_runs, "get_masky_provider", lambda: OnceProvider())

        db = SessionLocal()
        record = models.MaskyRun(idea_text="An idea long enough to pass validation here")
        db.add(record)
        db.commit()
        db.refresh(record)

        decision_ready = {"status": "decision_ready", "recommendation": "PROCEED"}
        api_runs._maybe_notify_masky(db, record, decision_ready)
        db.refresh(record)
        assert record.masky_notify_status == outcome_status
        assert len(calls) == 1

        api_runs._maybe_notify_masky(db, record, decision_ready)  # second attempt, e.g. a later poll
        db.close()
        assert len(calls) == 1  # still exactly one -- no automatic retry


# ---------- genuine concurrency: barrier-synchronized, independent sessions ----------

def test_concurrent_notify_calls_result_in_exactly_one_provider_invocation(monkeypatch):
    """5+ threads, each with its OWN database Session (as real concurrent
    GET /api/runs/{id} requests would have), all racing on the SAME run via
    a threading.Barrier so they genuinely overlap instead of a sleep-based
    approximation of a race."""
    import threading

    import api_runs
    import masky_adapter
    import models
    from database import SessionLocal

    N = 8
    calls = []
    calls_lock = threading.Lock()

    class CountingMock(masky_adapter.MockMaskyProvider):
        def notify_result(self, run_id, result):
            with calls_lock:
                calls.append(run_id)
            return super().notify_result(run_id, result)

    monkeypatch.setattr(api_runs, "get_masky_provider", lambda: CountingMock())

    setup_db = SessionLocal()
    record = models.MaskyRun(idea_text="An idea long enough to pass validation here")
    setup_db.add(record)
    setup_db.commit()
    setup_db.refresh(record)
    run_id = record.run_id
    setup_db.close()

    decision_ready = {"status": "decision_ready", "recommendation": "PROCEED"}
    barrier = threading.Barrier(N)
    errors = []

    def worker():
        db = SessionLocal()  # independent session per "request", like a real GET handler
        try:
            own_record = db.query(models.MaskyRun).filter(models.MaskyRun.run_id == run_id).first()
            barrier.wait(timeout=5)  # force genuinely simultaneous entry, no sleep-based timing
            api_runs._maybe_notify_masky(db, own_record, decision_ready)
        except Exception as e:  # noqa: BLE001 -- surface any worker exception to the main thread
            errors.append(e)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert len(calls) == 1

    verify_db = SessionLocal()
    final = verify_db.query(models.MaskyRun).filter(models.MaskyRun.run_id == run_id).first()
    assert final.masky_notify_status == "mocked"
    verify_db.close()


def test_concurrent_notify_calls_with_a_slow_provider_still_invoke_it_once(monkeypatch):
    """Same race, but the provider call itself is slow (simulating real
    network latency to masky.ai), to prove the claim is held for the whole
    duration of notify_result(), not just the instant of the UPDATE."""
    import threading
    import time as time_module

    import api_runs
    import masky_adapter
    import models
    from database import SessionLocal

    N = 6
    calls = []
    calls_lock = threading.Lock()

    class SlowMock(masky_adapter.MockMaskyProvider):
        def notify_result(self, run_id, result):
            with calls_lock:
                calls.append(run_id)
            time_module.sleep(0.1)
            return super().notify_result(run_id, result)

    monkeypatch.setattr(api_runs, "get_masky_provider", lambda: SlowMock())

    setup_db = SessionLocal()
    record = models.MaskyRun(idea_text="An idea long enough to pass validation here")
    setup_db.add(record)
    setup_db.commit()
    setup_db.refresh(record)
    run_id = record.run_id
    setup_db.close()

    decision_ready = {"status": "decision_ready", "recommendation": "PROCEED"}
    barrier = threading.Barrier(N)

    def worker():
        db = SessionLocal()
        own_record = db.query(models.MaskyRun).filter(models.MaskyRun.run_id == run_id).first()
        barrier.wait(timeout=5)
        api_runs._maybe_notify_masky(db, own_record, decision_ready)
        db.close()

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(calls) == 1


def test_an_unexpected_provider_exception_leaves_the_claim_in_progress_and_is_not_retried(monkeypatch):
    """If a provider implementation has a bug and raises instead of
    returning a result dict, the claim must not be lost/reclaimed (that
    would risk a duplicate real call on the next poll) -- it stays
    'in_progress' for manual follow-up, and GET must not 500 because of it."""
    import api_runs
    import models
    from database import SessionLocal

    class BrokenProvider:
        name = "real_unverified"

        def notify_result(self, run_id, result):
            raise RuntimeError("simulated provider bug")

    monkeypatch.setattr(api_runs, "get_masky_provider", lambda: BrokenProvider())

    db = SessionLocal()
    record = models.MaskyRun(idea_text="An idea long enough to pass validation here")
    db.add(record)
    db.commit()
    db.refresh(record)

    decision_ready = {"status": "decision_ready", "recommendation": "PROCEED"}
    api_runs._maybe_notify_masky(db, record, decision_ready)  # must not raise
    db.refresh(record)
    assert record.masky_notify_status == "in_progress"

    # A second call must not reclaim it -- the row stays stuck until a
    # human intervenes, by design.
    calls = []

    class ShouldNeverRun:
        def notify_result(self, run_id, result):
            calls.append(run_id)
            raise AssertionError("must not be called for an already-claimed run")

    monkeypatch.setattr(api_runs, "get_masky_provider", lambda: ShouldNeverRun())
    api_runs._maybe_notify_masky(db, record, decision_ready)
    db.close()
    assert calls == []
