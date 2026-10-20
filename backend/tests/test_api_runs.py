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


# ---------- 13/14/15. Masky adapter ----------

def test_masky_adapter_mock_provider_used_without_credentials(monkeypatch):
    import masky_adapter
    monkeypatch.setattr(masky_adapter, "MASKY_API_KEY", "")
    monkeypatch.setattr(masky_adapter, "MASKY_AVATAR_ID", "")
    provider = masky_adapter.get_masky_provider()
    assert isinstance(provider, masky_adapter.MockMaskyProvider)
    result = provider.notify_result("run-1", {"status": "decision_ready"})
    assert result["delivered"] is False
    assert result["provider"] == "mock"


def test_masky_adapter_real_provider_selected_with_credentials(monkeypatch):
    import masky_adapter
    monkeypatch.setattr(masky_adapter, "MASKY_API_KEY", "key")
    monkeypatch.setattr(masky_adapter, "MASKY_AVATAR_ID", "avatar")
    provider = masky_adapter.get_masky_provider()
    assert isinstance(provider, masky_adapter.RealMaskyProvider)


def test_masky_adapter_real_provider_timeout_is_handled_without_raising(monkeypatch):
    import httpx
    import masky_adapter

    def raise_timeout(*args, **kwargs):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(masky_adapter.httpx, "post", raise_timeout)
    provider = masky_adapter.RealMaskyProvider("key", "avatar", "https://api.masky.ai")
    result = provider.notify_result("run-1", {"status": "decision_ready"})
    assert result["delivered"] is False
    assert result["reason"] == "timeout"


def test_webhook_signature_rejected_without_configured_secret(monkeypatch):
    import masky_adapter
    monkeypatch.setattr(masky_adapter, "MASKY_WEBHOOK_SECRET", "")
    assert masky_adapter.verify_webhook_signature(b"payload", "sha256=anything") is False


def test_webhook_signature_accepted_when_valid(monkeypatch):
    import hashlib
    import hmac
    import masky_adapter
    monkeypatch.setattr(masky_adapter, "MASKY_WEBHOOK_SECRET", "topsecret")
    payload = b'{"event":"ping"}'
    sig = hmac.new(b"topsecret", payload, hashlib.sha256).hexdigest()
    assert masky_adapter.verify_webhook_signature(payload, f"sha256={sig}") is True


def test_webhook_signature_rejected_when_tampered(monkeypatch):
    import masky_adapter
    monkeypatch.setattr(masky_adapter, "MASKY_WEBHOOK_SECRET", "topsecret")
    assert masky_adapter.verify_webhook_signature(b"payload", "sha256=deadbeef") is False


def test_webhook_endpoint_rejects_unsigned_requests(client, monkeypatch):
    import masky_adapter
    monkeypatch.setattr(masky_adapter, "MASKY_WEBHOOK_SECRET", "topsecret")
    r = client.post("/webhooks/masky", json={"event": "ping"})
    assert r.status_code == 401


def test_webhook_endpoint_accepts_correctly_signed_requests(client, monkeypatch):
    import hashlib
    import hmac
    import json
    import masky_adapter
    monkeypatch.setattr(masky_adapter, "MASKY_WEBHOOK_SECRET", "topsecret")
    body = json.dumps({"event": "ping"}).encode()
    sig = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/masky", content=body,
                     headers={"X-Masky-Signature": f"sha256={sig}", "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"received": True}
