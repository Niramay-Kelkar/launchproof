"""
ResearchProvider interface. Exactly one real implementation should
be wired in before demo day; everything else stays a labeled mock.
Swap `USE_REAL_ADAPTER` or extend `real_research()` once a sponsor
tool is actually smoke-tested.
"""
import logging
import os
import re

import glasser_client
from launchproof.network import FetchError, PublicWeb

logger = logging.getLogger("launchproof.research_adapter")

USE_REAL_ADAPTER = os.environ.get("RESEARCH_PROVIDER_IS_REAL", "false").lower() == "true"

# Only https:// URLs are ever fetched -- either pasted directly into the idea
# text by the founder, or discovered via Glasser (see glasser_client.py,
# gated independently by GLASSER_ENABLED). Anything else (bare domains,
# http://, non-URL text) is left alone rather than guessed at. Pasted and
# discovered URLs are merged, deduplicated, and capped together at MAX_URLS
# -- one overall limit, not one per source -- before anything is fetched.
URL_PATTERN = re.compile(r"https://[^\s,)\"'<>]+")
MAX_URLS = 5

_web = PublicWeb()


def fixture_research(idea_text: str):
    """Clearly-labeled mock research. Replace with a real call behind
    the same return shape once an adapter is verified."""
    return [
        {
            "claim": f"Several existing products address problems adjacent to: {idea_text}",
            "source_url": None,
            "source_title": None,
            "excerpt": None,
            "evidence_type": "inference",
            "confidence": "low",
            "is_mock": 1,
            "source": None,
        },
    ]


def extract_urls(idea_text: str) -> list[str]:
    """Pull distinct https:// URLs out of the idea text, in order, capped at
    MAX_URLS. Trailing punctuation that regularly rides along with a URL in
    prose (a period, comma, closing paren) is stripped."""
    urls = []
    for match in URL_PATTERN.findall(idea_text):
        url = match.rstrip(".,;:)")
        if url not in urls:
            urls.append(url)
        if len(urls) >= MAX_URLS:
            break
    return urls


def merge_and_cap_urls(*labeled_url_lists: tuple[str, list[str]]) -> list[tuple[str, str]]:
    """Merge any number of (source_label, urls) pairs in order, dedupe
    (trailing-slash- and case-insensitive, first source seen wins), and apply
    ONE overall cap across all sources -- founder-pasted and Glasser-discovered
    URLs are not capped separately. Returns (url, source_label) pairs so
    callers can tell which source each surviving URL came from."""
    merged = []
    seen = set()
    for source, urls in labeled_url_lists:
        for url in urls:
            key = url.rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                merged.append((url, source))
    return merged[:MAX_URLS]


def real_research(idea_text: str):
    """Fetches up to MAX_URLS candidate pages through network.PublicWeb --
    the same pinned-TLS, private-IP-blocking, robots.txt-aware fetcher the
    launchproof graph package uses for its own research agents. Candidates
    come from two sources, merged and capped together:

      1. https:// URLs the founder pasted directly into the idea text.
      2. URLs discovered via Glasser (glasser_client.discover_urls), the one
         real sponsor integration -- a no-op unless GLASSER_ENABLED=true and
         GLASSER_API_KEY is set; any Glasser failure (disabled, no balance,
         timeout, provider error, no usable results) degrades silently to
         (1) alone, never to invented evidence.

    One evidence item per page that actually fetches. A candidate that fails
    -- blocked by SSRF/policy, disallowed by robots.txt, 404, etc. -- is
    skipped rather than faked, whichever source it came from. An idea with
    no reachable sources comes back as an empty list, same as "no evidence
    found" from the fixture path."""
    try:
        discovered = glasser_client.discover_urls(idea_text)
    except Exception:
        # discover_urls() is documented to never raise, but this adapter must
        # stay safe even if that contract is ever violated by a bug -- a
        # Glasser failure must never take founder-pasted research down with it.
        logger.exception("glasser_client.discover_urls raised unexpectedly; "
                          "continuing with founder-pasted URLs only")
        discovered = []
    candidates = merge_and_cap_urls(("founder_pasted", extract_urls(idea_text)), ("glasser", discovered))

    results = []
    for url, source in candidates:
        try:
            page = _web.fetch(url)
        except FetchError:
            continue
        results.append({
            "claim": f"\"{page.title}\" is a live, publicly reachable page related to this idea.",
            "source_url": page.url,
            "source_title": page.title,
            "excerpt": page.text[:500] or None,
            "evidence_type": "fact",
            "confidence": "medium",
            "is_mock": 0,
            "source": source,
        })
    return results


def run_research(idea_text: str):
    if USE_REAL_ADAPTER:
        return real_research(idea_text)
    return fixture_research(idea_text)
