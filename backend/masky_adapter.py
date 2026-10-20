"""
Outbound adapter toward Masky.AI. Masky/Avery is the CALLER of this backend's
/api/runs endpoints (start a run, poll for the result); this module is only
for the optional reverse direction -- best-effort proactively telling Avery
to speak a run's result, instead of making Avery poll forever.

RealMaskyProvider targets the documented endpoints at https://masky.ai/api
(fetched and verified against masky.ai/developer and masky.ai/api/docs):
  POST /conversations                      {avatarId, avatarOwnerUserId}
  POST /conversations/{conversationId}/turn {userText, mode: "speak", output: "text"}
Text-only output only -- audio/video turns are billed per second of
speech and are intentionally not used here. This has been verified against
the documentation but has NOT been exercised against the live API; treat it
as unverified-but-contract-correct until an actual live call succeeds, the
same way research_adapter.py treats RESEARCH_PROVIDER_IS_REAL.

Duplicate-call safety: neither POST /conversations nor .../turn documents
any idempotency key or safe-retry behavior (unlike /avatars/{id}/speak,
which does document request deduplication -- that's a different endpoint).
So a timeout at either step is treated as a permanently uncertain outcome,
never auto-retried -- retrying blind here risks creating a second
conversation or a second spoken turn with no way to know the first one
didn't already land.
"""
from __future__ import annotations

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
# Required by the documented POST /conversations contract alongside
# avatarId. Not guessed from anything else -- see README for how to obtain it.
MASKY_AVATAR_OWNER_USER_ID = os.environ.get("MASKY_AVATAR_OWNER_USER_ID", "")
MASKY_API_BASE_URL = os.environ.get("MASKY_API_BASE_URL", "https://masky.ai/api")
PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL", "")

_HTTP_TIMEOUT_SECONDS = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(*, run_id: str, action: str, status: str, provider: str,
              error: str | None = None, source: str = "masky") -> None:
    """Structured log line in the exact shape required by the integration
    spec. Never pass an api_key, an Authorization header, or conversation
    text/content into `error` or anywhere else here -- only status codes,
    short error-class strings, and ids."""
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


def _summarize_for_speech(result: dict[str, Any]) -> str:
    """A short, spoken-friendly line for Avery -- never the full JSON, and
    never inventing numbers the result doesn't already carry."""
    status = result.get("status")
    recommendation = result.get("recommendation")
    summary = result.get("summary")
    if recommendation:
        return f"Validation result: {recommendation}. {summary or ''}".strip()
    return f"Validation run status: {status}. {summary or ''}".strip()


class MaskyProvider(ABC):
    """What the backend needs from a Masky integration: tell Avery a run
    reached a terminal state. Never raises -- a Masky-side failure must
    never take down the validation run it is reporting on."""

    name: str

    @abstractmethod
    def notify_result(self, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
        ...


class MockMaskyProvider(MaskyProvider):
    """Explicitly labeled mock: logs what would have been sent, sends
    nothing. Default whenever real Masky configuration is incomplete."""

    name = "mock"

    def notify_result(self, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
        log_event(run_id=run_id, action="masky_notify_result", status="success", provider=self.name)
        logger.info("MOCK Masky provider: would have had Avery speak run %s "
                    "(status=%s) -- no real Masky call was made", run_id, result.get("status"))
        return {"provider": self.name, "status": "mocked", "delivered": False,
                "reason": "mock provider, no real Masky API call made"}


class RealMaskyProvider(MaskyProvider):
    """POSTs to the documented Masky conversation/turn endpoints. Contract
    verified against masky.ai/api/docs; NOT yet exercised against the live
    API -- do not treat a 'delivered' result as proof the real integration
    works end-to-end until it has actually been run once, live, with
    explicit sign-off."""

    name = "real_unverified"

    def __init__(self, api_key: str, avatar_id: str, avatar_owner_user_id: str, base_url: str):
        self.api_key = api_key
        self.avatar_id = avatar_id
        self.avatar_owner_user_id = avatar_owner_user_id
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _create_conversation(self, run_id: str) -> tuple[str | None, dict[str, Any] | None]:
        """Returns (conversation_id, error_result). error_result is None on
        success. Never retried -- see module docstring."""
        try:
            response = httpx.post(
                f"{self.base_url}/conversations",
                json={"avatarId": self.avatar_id, "avatarOwnerUserId": self.avatar_owner_user_id},
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException:
            log_event(run_id=run_id, action="masky_notify_result", status="uncertain", provider=self.name,
                      error="conversation creation timed out; outcome unknown, not retrying")
            return None, {"provider": self.name, "status": "uncertain", "delivered": False,
                          "reason": "conversation_creation_timeout"}
        except httpx.HTTPError as e:
            log_event(run_id=run_id, action="masky_notify_result", status="failure", provider=self.name,
                      error=f"conversation creation network error: {type(e).__name__}")
            return None, {"provider": self.name, "status": "failed", "delivered": False,
                          "reason": "conversation_creation_network_error"}

        if response.status_code in (401, 403):
            log_event(run_id=run_id, action="masky_notify_result", status="failure", provider=self.name,
                      error=f"conversation creation auth error {response.status_code}")
            return None, {"provider": self.name, "status": "failed", "delivered": False,
                          "reason": "authentication_failed"}
        if response.status_code >= 400:
            log_event(run_id=run_id, action="masky_notify_result", status="failure", provider=self.name,
                      error=f"conversation creation http {response.status_code}")
            return None, {"provider": self.name, "status": "failed", "delivered": False,
                          "reason": f"conversation_creation_http_{response.status_code}"}

        try:
            conversation_id = response.json()["conversationId"]
        except (ValueError, KeyError):
            log_event(run_id=run_id, action="masky_notify_result", status="failure", provider=self.name,
                      error="conversation creation returned a malformed response")
            return None, {"provider": self.name, "status": "failed", "delivered": False,
                          "reason": "malformed_conversation_response"}

        return conversation_id, None

    def _send_turn(self, run_id: str, conversation_id: str, text: str) -> dict[str, Any]:
        """Never retried -- see module docstring. Always carries
        conversation_id in its result, since the conversation itself
        definitely exists by this point regardless of how this call ends."""
        try:
            response = httpx.post(
                f"{self.base_url}/conversations/{conversation_id}/turn",
                json={"userText": text, "mode": "speak", "output": "text"},
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException:
            log_event(run_id=run_id, action="masky_notify_result", status="uncertain", provider=self.name,
                      error="turn request timed out after conversation was created; outcome unknown, not retrying")
            return {"provider": self.name, "status": "uncertain", "delivered": False,
                    "conversation_id": conversation_id, "reason": "turn_timeout"}
        except httpx.HTTPError as e:
            log_event(run_id=run_id, action="masky_notify_result", status="failure", provider=self.name,
                      error=f"turn network error: {type(e).__name__}")
            return {"provider": self.name, "status": "failed", "delivered": False,
                    "conversation_id": conversation_id, "reason": "turn_network_error"}

        if response.status_code in (401, 403):
            log_event(run_id=run_id, action="masky_notify_result", status="failure", provider=self.name,
                      error=f"turn auth error {response.status_code}")
            return {"provider": self.name, "status": "failed", "delivered": False,
                    "conversation_id": conversation_id, "reason": "authentication_failed"}
        if response.status_code >= 400:
            log_event(run_id=run_id, action="masky_notify_result", status="failure", provider=self.name,
                      error=f"turn http {response.status_code}")
            return {"provider": self.name, "status": "failed", "delivered": False,
                    "conversation_id": conversation_id, "reason": f"turn_http_{response.status_code}"}

        log_event(run_id=run_id, action="masky_notify_result", status="success", provider=self.name)
        return {"provider": self.name, "status": "delivered", "delivered": True,
                "conversation_id": conversation_id}

    def notify_result(self, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
        text = _summarize_for_speech(result)
        conversation_id, error_result = self._create_conversation(run_id)
        if error_result is not None:
            return error_result
        return self._send_turn(run_id, conversation_id, text)


def get_masky_provider() -> MaskyProvider:
    """Mock unless all three real-provider inputs are actually configured --
    avatarOwnerUserId is required by the documented /conversations contract,
    so its absence must not be silently worked around. Never silently
    claims to be real -- `provider` in every log line says which one ran."""
    if MASKY_API_KEY and MASKY_AVATAR_ID and MASKY_AVATAR_OWNER_USER_ID:
        return RealMaskyProvider(MASKY_API_KEY, MASKY_AVATAR_ID, MASKY_AVATAR_OWNER_USER_ID, MASKY_API_BASE_URL)
    return MockMaskyProvider()
