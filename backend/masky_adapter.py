"""
Outbound adapter toward Masky.AI. Masky/Avery is the CALLER of this backend's
/api/runs endpoints (start a run, poll for the result); this module is only
for the optional reverse direction -- best-effort proactively notifying Masky
when a run reaches a terminal state, instead of making Avery poll forever.

No real Masky API contract was provided or verified against. MockMaskyProvider
is the default and only one actually exercised end-to-end here.
RealMaskyProvider is an interface-shaped stub: it POSTs to a configurable
Masky endpoint with the documented env vars, but its request shape is a
best guess, not a verified integration. Treat it as unimplemented until it
has been smoke-tested against Masky's real API, the same way research_adapter.py
treats RESEARCH_PROVIDER_IS_REAL.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("launchproof.masky_adapter")

MASKY_API_KEY = os.environ.get("MASKY_API_KEY", "")
MASKY_AVATAR_ID = os.environ.get("MASKY_AVATAR_ID", "")
MASKY_WEBHOOK_SECRET = os.environ.get("MASKY_WEBHOOK_SECRET", "")
MASKY_API_BASE_URL = os.environ.get("MASKY_API_BASE_URL", "https://api.masky.ai")
PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL", "")

_HTTP_TIMEOUT_SECONDS = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(*, run_id: str, action: str, status: str, provider: str,
              error: str | None = None, source: str = "masky") -> None:
    """Structured log line in the exact shape required by the integration spec."""
    record = {
        "run_id": run_id,
        "source": source,
        "action": action,
        "timestamp": _now_iso(),
        "status": status,
        "provider": provider,
        "error": error,
    }
    logger.info(json.dumps(record))


class MaskyProvider(ABC):
    """What the backend needs from a Masky integration: notify Avery that a
    run reached a terminal state. Never raises -- a Masky-side failure must
    never take down the validation run it is reporting on."""

    name: str

    @abstractmethod
    def notify_result(self, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
        ...


class MockMaskyProvider(MaskyProvider):
    """Explicitly labeled mock: logs what would have been sent, sends nothing.
    This is the default whenever MASKY_API_KEY is unset -- i.e. whenever real
    Masky credentials have not been configured."""

    name = "mock"

    def notify_result(self, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
        log_event(run_id=run_id, action="masky_notify_result", status="success",
                  provider=self.name)
        logger.info("MOCK Masky provider: would have notified Avery for run %s with "
                    "status=%s recommendation=%s (no real Masky call was made)",
                    run_id, result.get("status"), result.get("recommendation"))
        return {"provider": self.name, "delivered": False, "reason": "mock provider, no real Masky API call made"}


class RealMaskyProvider(MaskyProvider):
    """Best-effort HTTP call to Masky's API. UNVERIFIED: the endpoint path and
    payload shape below are a placeholder, not something confirmed against
    Masky's actual documentation. Do not treat a 'success' log from this
    provider as proof the real integration works end-to-end."""

    name = "real_unverified"

    def __init__(self, api_key: str, avatar_id: str, base_url: str):
        self.api_key = api_key
        self.avatar_id = avatar_id
        self.base_url = base_url.rstrip("/")

    def notify_result(self, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
        payload = {"avatar_id": self.avatar_id, "run_id": run_id, "result": result}
        try:
            response = httpx.post(
                f"{self.base_url}/v1/avatars/{self.avatar_id}/events",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            log_event(run_id=run_id, action="masky_notify_result", status="success", provider=self.name)
            return {"provider": self.name, "delivered": True, "status_code": response.status_code}
        except httpx.TimeoutException as e:
            log_event(run_id=run_id, action="masky_notify_result", status="failure",
                      provider=self.name, error=f"timeout: {e}")
            return {"provider": self.name, "delivered": False, "reason": "timeout"}
        except httpx.HTTPError as e:
            log_event(run_id=run_id, action="masky_notify_result", status="failure",
                      provider=self.name, error=str(e))
            return {"provider": self.name, "delivered": False, "reason": str(e)}


def get_masky_provider() -> MaskyProvider:
    """Mock unless real credentials are actually configured. Never silently
    claims to be real -- `provider` in every log line says which one ran."""
    if MASKY_API_KEY and MASKY_AVATAR_ID:
        return RealMaskyProvider(MASKY_API_KEY, MASKY_AVATAR_ID, MASKY_API_BASE_URL)
    return MockMaskyProvider()


def verify_webhook_signature(payload: bytes, signature_header: str | None) -> bool:
    """HMAC-SHA256 signature check for an inbound Masky webhook, using
    MASKY_WEBHOOK_SECRET. Returns False (reject) if the secret is not
    configured -- an unconfigured secret must never be treated as 'anything
    goes'. Expected header shape: 'sha256=<hex digest>' (GitHub/Stripe-style)."""
    if not MASKY_WEBHOOK_SECRET:
        return False
    if not signature_header:
        return False
    expected = hmac.new(MASKY_WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)
