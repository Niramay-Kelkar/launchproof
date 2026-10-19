"""
Tests for glasser_client.py. No real Glasser calls are ever made here --
every HTTP interaction goes through httpx.MockTransport via a handler
function, injected through the glasser_client._TRANSPORT test seam.
"""
import httpx
import pytest

import glasser_client


SEARCH_RESPONSE_GOOGLE = {
    "data": [
        {
            "provider": "serper", "provider_label": "Serper",
            "endpoint": "/endpoints/serper/search-google-web-results",
            "endpoint_version": 1,
            "name": "Search Google Web Results",
            "description": "Google web search via Serper",
            "tags": ["google", "search", "web"],
            "price": {"rule": {"type": "flat", "amount_usd": "0.01"},
                      "display": {"amount_usd": "0.01", "unit": "per_call"},
                      "charges": {"NO_RESULT": "0", "PROVIDER_ERROR": "0",
                                  "TIMED_OUT": "0", "INTERNAL": "0"}},
            "run_mode": "sync",
            "timeout_ms": 10000,
            "score": 0.9,
        },
        {
            "provider": "dataforseo", "provider_label": "DataForSEO",
            "endpoint": "/endpoints/dataforseo/organic-results",
            "endpoint_version": 1,
            "name": "Organic Google Results", "description": "DataForSEO organic SERP",
            "tags": ["google", "seo"],
            "price": {"rule": {"type": "flat", "amount_usd": "0.02"},
                      "display": {"amount_usd": "0.02", "unit": "per_call"},
                      "charges": {"NO_RESULT": "0", "PROVIDER_ERROR": "0",
                                  "TIMED_OUT": "0", "INTERNAL": "0"}},
            "run_mode": "async",
            "timeout_ms": None,
            "score": 0.5,
        },
    ],
    "next_cursor": None, "total": 2, "task_id": "t1",
}

INSPECT_RESPONSE = {
    "provider": "serper", "provider_label": "Serper",
    "endpoint": "/endpoints/serper/search-google-web-results",
    "endpoint_version": 1,
    "name": "Search Google Web Results", "description": "Google web search via Serper",
    "tags": ["google", "search", "web"],
    "price": {"rule": {"type": "flat", "amount_usd": "0.01"},
              "display": {"amount_usd": "0.01", "unit": "per_call"},
              "charges": {"NO_RESULT": "0", "PROVIDER_ERROR": "0",
                          "TIMED_OUT": "0", "INTERNAL": "0"}},
    "run_mode": "sync",
    "method": "POST",
    "input_schema": {"type": "object"},
    "output_schema": None,
    "output_schema_source": None,
    "sample_input": {"q": "example query", "gl": "us", "hl": "en", "num": 10},
    "doc_url": None,
}


def run_response(run_id, status="COMPLETED", output=None, charge="0.01"):
    return {
        "id": run_id, "run_url": f"https://api.glasser.ai/v1/runs/{run_id}",
        "provider": "serper", "endpoint": "/endpoints/serper/search-google-web-results",
        "endpoint_version": 1, "status": status,
        "failure": None if status == "COMPLETED" else {"kind": "INTERNAL", "message": "boom"},
        "input": {}, "output": output, "provider_response": None,
        "charge_usd": charge, "charge_basis": None, "stoppable": False,
        "stop_requested_at": None, "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:00:01Z",
        "task_id": "t1", "metadata": {},
    }


SERPER_OUTPUT = {
    "organic": [
        {"title": "Example Competitor", "link": "https://example.com/competitor", "snippet": "..."},
        {"title": "Another One", "link": "https://example.org/alt", "snippet": "..."},
    ]
}


@pytest.fixture(autouse=True)
def enable_glasser(monkeypatch):
    monkeypatch.setenv("GLASSER_ENABLED", "true")
    monkeypatch.setenv("GLASSER_API_KEY", "test-key")
    monkeypatch.setattr(glasser_client.time, "sleep", lambda *_: None)
    yield
    glasser_client._TRANSPORT = None


def install_transport(monkeypatch, handler):
    monkeypatch.setattr(glasser_client, "_TRANSPORT", httpx.MockTransport(handler))


def json_response(status_code, payload):
    return httpx.Response(status_code, json=payload)


# ---------- disabled / unconfigured ----------

def test_disabled_returns_empty_without_any_http_call(monkeypatch):
    monkeypatch.setenv("GLASSER_ENABLED", "false")

    def handler(request):
        raise AssertionError("no HTTP call should be made when disabled")

    install_transport(monkeypatch, handler)
    assert glasser_client.discover_urls("anything") == []


def test_missing_api_key_returns_empty_without_any_http_call(monkeypatch):
    monkeypatch.setenv("GLASSER_API_KEY", "")

    def handler(request):
        raise AssertionError("no HTTP call should be made with no API key")

    install_transport(monkeypatch, handler)
    assert glasser_client.discover_urls("anything") == []


# ---------- successful discovery ----------

def test_successful_discovery_selects_google_endpoint_and_harvests_urls(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/endpoints/search":
            return json_response(200, SEARCH_RESPONSE_GOOGLE)
        if request.url.path == "/v1/endpoints/inspect":
            return json_response(200, INSPECT_RESPONSE)
        if request.url.path == "/v1/runs":
            assert request.headers.get("idempotency-key")
            return json_response(200, run_response("run-1", output=SERPER_OUTPUT))
        raise AssertionError(f"unexpected path {request.url.path}")

    install_transport(monkeypatch, handler)
    urls = glasser_client.discover_urls("A weekly meal planner for shift workers")

    assert urls == ["https://example.com/competitor", "https://example.org/alt"]
    # 1 search + 1 inspect + MAX_QUERIES run calls, never more
    assert calls.count("/v1/endpoints/search") == 1
    assert calls.count("/v1/endpoints/inspect") == 1
    assert calls.count("/v1/runs") == glasser_client.MAX_QUERIES


def test_prefers_sync_endpoint_over_async_when_both_are_google_tagged(monkeypatch):
    seen_providers = []

    def handler(request):
        if request.url.path == "/v1/endpoints/search":
            return json_response(200, SEARCH_RESPONSE_GOOGLE)
        if request.url.path == "/v1/endpoints/inspect":
            import json
            seen_providers.append(json.loads(request.content)["provider"])
            return json_response(200, INSPECT_RESPONSE)
        if request.url.path == "/v1/runs":
            return json_response(200, run_response("run-1", output={"organic": []}))
        raise AssertionError("unexpected path")

    install_transport(monkeypatch, handler)
    glasser_client.discover_urls("idea")
    assert seen_providers == ["serper"]  # the sync one, not dataforseo (async)


# ---------- duplicate URLs ----------

def test_duplicate_urls_across_queries_are_deduped(monkeypatch):
    def handler(request):
        if request.url.path == "/v1/endpoints/search":
            return json_response(200, SEARCH_RESPONSE_GOOGLE)
        if request.url.path == "/v1/endpoints/inspect":
            return json_response(200, INSPECT_RESPONSE)
        if request.url.path == "/v1/runs":
            # every query happens to surface the same URL (with/without trailing slash)
            return json_response(200, run_response("run-x", output={
                "organic": [{"title": "Dup", "link": "https://example.com/dup/"}]
            }))
        raise AssertionError("unexpected path")

    install_transport(monkeypatch, handler)
    urls = glasser_client.discover_urls("idea")
    assert urls == ["https://example.com/dup/"]


# ---------- invalid / non-http(s) candidates ----------

def test_non_http_candidates_are_never_harvested(monkeypatch):
    def handler(request):
        if request.url.path == "/v1/endpoints/search":
            return json_response(200, SEARCH_RESPONSE_GOOGLE)
        if request.url.path == "/v1/endpoints/inspect":
            return json_response(200, INSPECT_RESPONSE)
        if request.url.path == "/v1/runs":
            return json_response(200, run_response("run-y", output={
                "organic": [
                    {"title": "mail", "link": "mailto:someone@example.com"},
                    {"title": "js", "link": "javascript:alert(1)"},
                    {"title": "good", "link": "https://example.com/good"},
                ]
            }))
        raise AssertionError("unexpected path")

    install_transport(monkeypatch, handler)
    urls = glasser_client.discover_urls("idea")
    assert urls == ["https://example.com/good"]


def test_private_ip_candidate_is_returned_by_discovery_but_rejected_by_safe_fetcher(monkeypatch):
    """discover_urls() only harvests strings; it never fetches, so a
    sponsor-discovered private/loopback URL is NOT its job to block -- that
    SSRF protection lives in the shared fetcher and is exercised end-to-end
    via research_adapter's merge in test_research_adapter.py."""
    def handler(request):
        if request.url.path == "/v1/endpoints/search":
            return json_response(200, SEARCH_RESPONSE_GOOGLE)
        if request.url.path == "/v1/endpoints/inspect":
            return json_response(200, INSPECT_RESPONSE)
        if request.url.path == "/v1/runs":
            return json_response(200, run_response("run-z", output={
                "organic": [{"title": "internal", "link": "https://127.0.0.1/admin"}]
            }))
        raise AssertionError("unexpected path")

    install_transport(monkeypatch, handler)
    urls = glasser_client.discover_urls("idea")
    assert urls == ["https://127.0.0.1/admin"]


# ---------- malformed responses ----------

def test_malformed_search_response_missing_data_returns_empty(monkeypatch):
    def handler(request):
        return json_response(200, {"next_cursor": None, "total": 0, "task_id": "t1"})

    install_transport(monkeypatch, handler)
    assert glasser_client.discover_urls("idea") == []


def test_no_google_tagged_endpoint_in_catalog_returns_empty(monkeypatch):
    def handler(request):
        return json_response(200, {"data": [{
            "provider": "bing", "endpoint": "/endpoints/bing/search", "endpoint_version": 1,
            "name": "Bing Search", "description": "not google", "tags": ["search"],
            "price": {}, "run_mode": "sync", "score": 1,
        }], "next_cursor": None, "total": 1, "task_id": "t1"})

    install_transport(monkeypatch, handler)
    assert glasser_client.discover_urls("idea") == []


def test_inspect_without_recognizable_query_field_returns_empty(monkeypatch):
    def handler(request):
        if request.url.path == "/v1/endpoints/search":
            return json_response(200, SEARCH_RESPONSE_GOOGLE)
        if request.url.path == "/v1/endpoints/inspect":
            bad = dict(INSPECT_RESPONSE)
            bad["sample_input"] = {"weird_field_1": "x", "weird_field_2": 2}
            return json_response(200, bad)
        raise AssertionError("should not reach /v1/runs")

    install_transport(monkeypatch, handler)
    assert glasser_client.discover_urls("idea") == []


# ---------- endpoint/provider errors ----------

def test_unauthorized_search_response_returns_empty(monkeypatch):
    def handler(request):
        return json_response(401, {"error": {"code": "unauthorized", "message": "bad key"},
                                    "request_id": "r1"})

    install_transport(monkeypatch, handler)
    assert glasser_client.discover_urls("idea") == []


def test_run_level_provider_error_skips_that_query_but_continues(monkeypatch):
    call_count = {"runs": 0}

    def handler(request):
        if request.url.path == "/v1/endpoints/search":
            return json_response(200, SEARCH_RESPONSE_GOOGLE)
        if request.url.path == "/v1/endpoints/inspect":
            return json_response(200, INSPECT_RESPONSE)
        if request.url.path == "/v1/runs":
            call_count["runs"] += 1
            if call_count["runs"] == 1:
                return json_response(200, run_response("run-fail", status="FAILED"))
            return json_response(200, run_response("run-ok", output={
                "organic": [{"title": "ok", "link": "https://example.com/ok"}]
            }))
        raise AssertionError("unexpected path")

    install_transport(monkeypatch, handler)
    urls = glasser_client.discover_urls("idea")
    assert urls == ["https://example.com/ok"]
    assert call_count["runs"] == glasser_client.MAX_QUERIES


def test_rate_limit_is_retried_once_then_succeeds(monkeypatch):
    attempts = {"search": 0}

    def handler(request):
        if request.url.path == "/v1/endpoints/search":
            attempts["search"] += 1
            if attempts["search"] == 1:
                return json_response(429, {"error": {"code": "rate_limited", "message": "slow down",
                                                       "retry_after_ms": 1}, "request_id": "r1"})
            return json_response(200, SEARCH_RESPONSE_GOOGLE)
        if request.url.path == "/v1/endpoints/inspect":
            return json_response(200, INSPECT_RESPONSE)
        if request.url.path == "/v1/runs":
            return json_response(200, run_response("run-1", output={"organic": []}))
        raise AssertionError("unexpected path")

    install_transport(monkeypatch, handler)
    assert glasser_client.discover_urls("idea") == []
    assert attempts["search"] == 2


# ---------- timeouts ----------

def test_timeout_on_search_returns_empty(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    install_transport(monkeypatch, handler)
    assert glasser_client.discover_urls("idea") == []


def test_timeout_on_one_run_skips_that_query_but_continues(monkeypatch):
    call_count = {"runs": 0}

    def handler(request):
        if request.url.path == "/v1/endpoints/search":
            return json_response(200, SEARCH_RESPONSE_GOOGLE)
        if request.url.path == "/v1/endpoints/inspect":
            return json_response(200, INSPECT_RESPONSE)
        if request.url.path == "/v1/runs":
            call_count["runs"] += 1
            if call_count["runs"] == 1:
                raise httpx.ReadTimeout("timed out", request=request)
            return json_response(200, run_response("run-ok", output={
                "organic": [{"title": "ok", "link": "https://example.com/ok"}]
            }))
        raise AssertionError("unexpected path")

    install_transport(monkeypatch, handler)
    urls = glasser_client.discover_urls("idea")
    assert urls == ["https://example.com/ok"]
    assert call_count["runs"] == glasser_client.MAX_QUERIES


def test_async_run_is_polled_until_terminal(monkeypatch):
    poll_count = {"n": 0}

    def handler(request):
        if request.url.path == "/v1/endpoints/search":
            return json_response(200, SEARCH_RESPONSE_GOOGLE)
        if request.url.path == "/v1/endpoints/inspect":
            return json_response(200, INSPECT_RESPONSE)
        if request.url.path == "/v1/runs":
            return json_response(202, run_response("run-async", status="QUEUED", output=None))
        if request.url.path == "/v1/runs/run-async":
            poll_count["n"] += 1
            if poll_count["n"] < 2:
                return json_response(200, run_response("run-async", status="RUNNING"))
            return json_response(200, run_response("run-async", status="COMPLETED", output={
                "organic": [{"title": "polled", "link": "https://example.com/polled"}]
            }))
        raise AssertionError(f"unexpected path {request.url.path}")

    install_transport(monkeypatch, handler)
    urls = glasser_client.discover_urls("idea")
    assert "https://example.com/polled" in urls
    assert poll_count["n"] >= 2


# ---------- budget enforcement ----------

def test_never_more_than_max_queries_run_calls_regardless_of_catalog_size(monkeypatch):
    call_count = {"runs": 0}

    def handler(request):
        if request.url.path == "/v1/endpoints/search":
            return json_response(200, SEARCH_RESPONSE_GOOGLE)
        if request.url.path == "/v1/endpoints/inspect":
            return json_response(200, INSPECT_RESPONSE)
        if request.url.path == "/v1/runs":
            call_count["runs"] += 1
            return json_response(200, run_response(f"run-{call_count['runs']}", output={"organic": []}))
        raise AssertionError("unexpected path")

    install_transport(monkeypatch, handler)
    glasser_client.discover_urls("idea")
    assert call_count["runs"] == 3 == glasser_client.MAX_QUERIES
