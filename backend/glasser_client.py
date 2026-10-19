"""
Glasser (https://glasser.ai) integration -- the one real sponsor integration
in LaunchProof. Every other sponsor tool stays a labeled mock until the team
explicitly decides otherwise.

This module only talks to Glasser's catalog/run API and returns candidate
URL strings. It never fetches page content itself -- that stays the job of
launchproof/network.py's SSRF-safe fetcher, called from research_adapter.py.
Keeping the split this way means a Glasser response, however malformed or
hostile, can only ever hand back a string; it can't skip the pinned-TLS,
private-IP, or robots.txt checks that real_research() already applies to
every candidate URL regardless of where it came from.

API contract (confirmed from https://glasser.ai/docs/api-reference/ on
2026-10-19 -- not guessed):
  POST /v1/endpoints/search   {use_case, query?, limit?, cursor?, task_id?, metadata?}
                              -> {data: [...], next_cursor, total, task_id}
  POST /v1/endpoints/inspect  {provider, endpoint, endpoint_version?, task_id?, metadata?}
                              -> {provider, endpoint, input_schema, sample_input,
                                  price, run_mode, timeout_ms, ...}
  POST /v1/runs               {provider, endpoint, endpoint_version?, input?, task_id?, metadata?}
                              header: idempotency-key (required)
                              -> 200 terminal run | 202 in-flight run (QUEUED/RUNNING)
  GET  /v1/runs/{runId}       -> same Run object, poll until COMPLETED/FAILED/STOPPED

Glasser's catalog returns provider-specific endpoint paths and input/output
shapes (e.g. Serper's `q`/`num`, DataForSEO's `keyword`/`depth`) that aren't
fixed across providers or documented as a stable contract. Rather than
hardcode one provider's field names, this module inspects the endpoint's
own `input_schema`/`sample_input` at call time and heuristically locates a
query-like and count-like field by name. If it can't confidently identify
either, it gives up on Glasser for that idea rather than guess and send a
malformed paid request.
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Any

import httpx

logger = logging.getLogger("launchproof.glasser")

API_BASE = "https://api.glasser.ai/v1"

# Hard, non-configurable call budget -- "start with at most three queries and
# a small result limit" is enforced here, not left to an env var someone
# could crank up.
MAX_QUERIES = 3
RESULTS_PER_QUERY = 3
MAX_POLLS = 5
POLL_INTERVAL_SECONDS = 1.0
QUERY_VARIANTS = ("", " competitors", " alternatives")[:MAX_QUERIES]

QUERY_KEY_HINTS = ("query", "q", "keyword", "search", "term", "phrase")
COUNT_KEY_HINTS = ("num", "count", "limit", "depth", "max_results", "results")
URL_IN_TEXT = re.compile(r"https?://[^\s,)\"'<>]+")

# Test seam only: production always uses real httpx networking (transport=None).
_TRANSPORT = None


class GlasserError(Exception):
    """Any Glasser call that failed, timed out, or came back unusable.
    Always caught inside discover_urls() -- never escapes to callers."""


def _config() -> dict:
    """Read live each call (not frozen at import) so tests can toggle env
    vars per-test and so an operator can flip GLASSER_ENABLED without a
    process restart being the only way it takes effect."""
    return {
        "enabled": os.environ.get("GLASSER_ENABLED", "false").lower() == "true",
        "api_key": os.environ.get("GLASSER_API_KEY", ""),
        "timeout": float(os.environ.get("GLASSER_HTTP_TIMEOUT_SECONDS", "10")),
    }


def _headers(api_key: str, idempotency_key: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if idempotency_key:
        headers["idempotency-key"] = idempotency_key
    return headers


def _safe_body(resp: httpx.Response) -> str:
    # Never log the Authorization header; only a clipped response body.
    try:
        return str(resp.json())[:300]
    except ValueError:
        return resp.text[:300]


def _retry_after_seconds(resp: httpx.Response) -> float:
    try:
        ms = resp.json().get("error", {}).get("retry_after_ms")
    except ValueError:
        ms = None
    return min((ms or 1000) / 1000, 2.0)


def _post(client: httpx.Client, path: str, api_key: str, json_body: dict,
          idempotency_key: str | None = None) -> dict:
    try:
        resp = client.post(f"{API_BASE}{path}", json=json_body, headers=_headers(api_key, idempotency_key))
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise GlasserError(f"{path} network failure: {exc}") from exc
    if resp.status_code == 429:
        time.sleep(_retry_after_seconds(resp))
        try:
            resp = client.post(f"{API_BASE}{path}", json=json_body, headers=_headers(api_key, idempotency_key))
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise GlasserError(f"{path} network failure on retry: {exc}") from exc
    if resp.status_code not in (200, 202):
        raise GlasserError(f"{path} returned HTTP {resp.status_code}: {_safe_body(resp)}")
    try:
        return resp.json()
    except ValueError as exc:
        raise GlasserError(f"{path} returned a non-JSON body") from exc


def _get(client: httpx.Client, path: str, api_key: str) -> dict:
    try:
        resp = client.get(f"{API_BASE}{path}", headers=_headers(api_key))
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise GlasserError(f"{path} network failure: {exc}") from exc
    if resp.status_code != 200:
        raise GlasserError(f"{path} returned HTTP {resp.status_code}: {_safe_body(resp)}")
    try:
        return resp.json()
    except ValueError as exc:
        raise GlasserError(f"{path} returned a non-JSON body") from exc


def _select_google_endpoint(data: list[dict]) -> dict | None:
    """Pick a Google web-search Endpoint from the catalog results. Glasser's
    catalog is provider-agnostic, so this looks for "google" in the name,
    description or tags rather than assuming any specific provider slug.
    Prefers a sync endpoint (no polling needed) when more than one matches."""
    def is_google(item: dict) -> bool:
        haystack = " ".join([
            str(item.get("name", "")), str(item.get("description", "")),
            *[str(t) for t in item.get("tags", []) or []],
        ]).lower()
        return "google" in haystack

    candidates = [item for item in data if isinstance(item, dict) and is_google(item)]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.get("run_mode") != "sync")
    return candidates[0]


def _find_key(sample_input: dict, hints: tuple[str, ...]) -> str | None:
    for key in sample_input:
        lowered = key.lower()
        if any(hint == lowered or hint in lowered for hint in hints):
            return key
    return None


def _harvest_urls(node: Any, found: list[str]) -> None:
    """Walk a provider's output (shape is provider-specific and not
    documented as a fixed contract) and pull out candidate URLs. Prefers
    values under an obviously link-shaped key, but falls back to scanning
    any string for an embedded http(s) URL, since output_schema varies."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and key.lower() in ("url", "link", "href") \
                    and value.startswith(("http://", "https://")):
                found.append(value)
            else:
                _harvest_urls(value, found)
    elif isinstance(node, list):
        for item in node:
            _harvest_urls(item, found)
    elif isinstance(node, str):
        found.extend(URL_IN_TEXT.findall(node))


def _await_terminal(client: httpx.Client, run: dict, api_key: str) -> dict:
    polls = 0
    while run.get("status") in ("QUEUED", "RUNNING") and polls < MAX_POLLS:
        time.sleep(POLL_INTERVAL_SECONDS)
        run = _get(client, f"/runs/{run['id']}", api_key)
        polls += 1
    return run


def discover_urls(idea_text: str) -> list[str]:
    """Best-effort: find a Google web-search Endpoint through Glasser,
    inspect its schema and price, run up to MAX_QUERIES small bounded
    searches derived from the idea text, and return deduplicated candidate
    URLs (not fetched content).

    Never raises. Disabled, missing key, provider error, timeout, no usable
    endpoint, or a malformed response all log a warning/info and return []
    so callers fall back to whatever founder-pasted URLs they already have.
    """
    cfg = _config()
    if not cfg["enabled"]:
        return []
    if not cfg["api_key"]:
        logger.warning("Glasser discovery is enabled but GLASSER_API_KEY is not set; skipping")
        return []

    task_id = f"launchproof-{uuid.uuid4().hex[:24]}"
    try:
        with httpx.Client(timeout=cfg["timeout"], transport=_TRANSPORT) as client:
            search_body = {
                "use_case": (
                    "Search the public web for existing products, competitors, and market "
                    f"information relevant to this startup idea: {idea_text[:300]}"
                ),
                "query": "google web search",
                "limit": 5,
                "task_id": task_id,
            }
            search_result = _post(client, "/endpoints/search", cfg["api_key"], search_body)
            data = search_result.get("data")
            if not isinstance(data, list):
                logger.info("Glasser: malformed /endpoints/search response (no 'data' list); skipping")
                return []

            endpoint = _select_google_endpoint(data)
            if not endpoint:
                logger.info("Glasser: no Google web-search endpoint found in catalog; skipping")
                return []
            try:
                provider = endpoint["provider"]
                endpoint_path = endpoint["endpoint"]
            except (KeyError, TypeError):
                logger.info("Glasser: catalog entry missing provider/endpoint fields; skipping")
                return []
            endpoint_version = endpoint.get("endpoint_version")

            inspect_body = {
                "provider": provider, "endpoint": endpoint_path,
                "endpoint_version": endpoint_version, "task_id": task_id,
            }
            inspected = _post(client, "/endpoints/inspect", cfg["api_key"], inspect_body)
            sample_input = inspected.get("sample_input")
            if not isinstance(sample_input, dict):
                logger.info("Glasser: endpoint %s%s has no usable sample_input; skipping", provider, endpoint_path)
                return []
            query_key = _find_key(sample_input, QUERY_KEY_HINTS)
            if not query_key:
                logger.info("Glasser: could not identify a query field on %s%s; skipping", provider, endpoint_path)
                return []
            count_key = _find_key(sample_input, COUNT_KEY_HINTS)
            price = inspected.get("price", {}).get("display", {}) if isinstance(inspected.get("price"), dict) else {}
            logger.info("Glasser: selected %s%s (run_mode=%s, price=%s %s)",
                        provider, endpoint_path, inspected.get("run_mode"),
                        price.get("amount_usd"), price.get("unit"))

            urls: list[str] = []
            base_query = idea_text.strip()[:180]
            for i, variant in enumerate(QUERY_VARIANTS):
                run_input = dict(sample_input)
                run_input[query_key] = base_query + variant
                if count_key:
                    try:
                        existing = int(run_input.get(count_key) or RESULTS_PER_QUERY)
                    except (TypeError, ValueError):
                        existing = RESULTS_PER_QUERY
                    run_input[count_key] = min(existing, RESULTS_PER_QUERY)
                run_body = {
                    "provider": provider, "endpoint": endpoint_path,
                    "endpoint_version": endpoint_version, "input": run_input, "task_id": task_id,
                }
                idempotency_key = f"{task_id}-run-{i}"
                try:
                    run = _post(client, "/runs", cfg["api_key"], run_body, idempotency_key=idempotency_key)
                    run = _await_terminal(client, run, cfg["api_key"])
                except GlasserError as exc:
                    logger.warning("Glasser: run %d/%d failed: %s", i + 1, len(QUERY_VARIANTS), exc)
                    continue
                if run.get("status") != "COMPLETED":
                    logger.warning("Glasser: run %s ended %s (%s)",
                                   run.get("id"), run.get("status"), run.get("failure"))
                    continue
                logger.info("Glasser: run %s completed, charge_usd=%s", run.get("id"), run.get("charge_usd"))
                found: list[str] = []
                _harvest_urls(run.get("output"), found)
                urls.extend(found)

            deduped: list[str] = []
            seen: set[str] = set()
            for url in urls:
                key = url.rstrip("/").lower()
                if key not in seen:
                    seen.add(key)
                    deduped.append(url)
            return deduped
    except GlasserError as exc:
        logger.warning("Glasser discovery failed, falling back to founder-pasted URLs only: %s", exc)
        return []
