import pytest

import glasser_client
import research_adapter
from launchproof.network import FetchError, Page, public_target


# Mirrors launchproof/tests/test_workflow.py::test_network_policy_blocks_unsafe_targets --
# same policy (pinned-TLS fetcher, no private/loopback/link-local targets, no
# credentials in the URL, no non-443 ports), now exercised from the backend side.
@pytest.mark.parametrize("url", ["http://example.com", "https://127.0.0.1/", "https://[::1]/",
                                 "https://user:pw@example.com/", "https://example.com:8443/",
                                 "https://169.254.169.254/latest/meta-data"])
def test_network_policy_blocks_unsafe_targets(url):
    with pytest.raises(FetchError):
        public_target(url)


def test_extract_urls_dedupes_strips_punctuation_and_caps():
    text = (
        "Check https://example.com/a, and https://example.com/a again, "
        "also (https://example.com/b) and https://example.com/c. "
        "https://example.com/d https://example.com/e https://example.com/f"
    )
    urls = research_adapter.extract_urls(text)
    assert urls == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
        "https://example.com/d",
        "https://example.com/e",
    ]
    assert len(urls) == research_adapter.MAX_URLS


def test_extract_urls_ignores_non_https_and_bare_domains():
    text = "Competitor at http://insecure.example.com and just plain example.com, no scheme."
    assert research_adapter.extract_urls(text) == []


def test_real_research_returns_empty_list_when_no_urls_present():
    assert research_adapter.real_research("A weekly meal planner for shift workers") == []


def test_real_research_skips_urls_that_fail_policy_or_fetch(monkeypatch):
    def fake_fetch(self, url):
        raise FetchError("blocked")

    monkeypatch.setattr(research_adapter._web, "fetch", fake_fetch.__get__(research_adapter._web))
    results = research_adapter.real_research("See https://example.com/blocked for reference")
    assert results == []


def test_real_research_builds_sourced_evidence_from_a_fetched_page(monkeypatch):
    def fake_fetch(self, url):
        assert url == "https://example.com/pricing"
        return Page(url=url, title="Example Pricing", text="Plans start at $10/month. " * 50)

    monkeypatch.setattr(research_adapter._web, "fetch", fake_fetch.__get__(research_adapter._web))
    results = research_adapter.real_research("Competitor: https://example.com/pricing")

    assert len(results) == 1
    item = results[0]
    assert item["source_url"] == "https://example.com/pricing"
    assert item["source_title"] == "Example Pricing"
    assert item["evidence_type"] == "fact"
    assert item["confidence"] == "medium"
    assert item["is_mock"] == 0
    assert item["excerpt"].startswith("Plans start at $10/month.")
    assert len(item["excerpt"]) <= 500
    assert "Example Pricing" in item["claim"]
    # same keys as the fixture path, so callers never have to branch on adapter
    assert set(item) == set(research_adapter.fixture_research("x")[0])


# ---------- Glasser merge / cap / fallback behavior ----------

def test_merge_and_cap_urls_dedupes_across_sources_and_applies_one_overall_cap():
    pasted = ["https://a.example.com/", "https://b.example.com"]
    discovered = ["https://b.example.com/", "https://c.example.com", "https://d.example.com",
                  "https://e.example.com", "https://f.example.com"]
    merged = research_adapter.merge_and_cap_urls(pasted, discovered)
    assert merged == [
        "https://a.example.com/", "https://b.example.com", "https://c.example.com",
        "https://d.example.com", "https://e.example.com",
    ]
    assert len(merged) == research_adapter.MAX_URLS


def test_real_research_is_unaffected_when_glasser_disabled(monkeypatch):
    """Default configuration: GLASSER_ENABLED is unset/false, so real_research
    behaves exactly as it did before Glasser existed -- founder-pasted URLs
    only, fetched the same way."""
    monkeypatch.delenv("GLASSER_ENABLED", raising=False)

    def fake_fetch(self, url):
        return Page(url=url, title="Pasted Page", text="pasted content")

    monkeypatch.setattr(research_adapter._web, "fetch", fake_fetch.__get__(research_adapter._web))
    results = research_adapter.real_research("See https://example.com/pasted for reference")
    assert len(results) == 1
    assert results[0]["source_url"] == "https://example.com/pasted"


def test_real_research_merges_glasser_discovered_urls_with_pasted_ones(monkeypatch):
    monkeypatch.setattr(glasser_client, "discover_urls",
                        lambda idea_text: ["https://discovered.example.com/found"])

    fetched = []

    def fake_fetch(self, url):
        fetched.append(url)
        return Page(url=url, title="Title for " + url, text="body")

    monkeypatch.setattr(research_adapter._web, "fetch", fake_fetch.__get__(research_adapter._web))
    results = research_adapter.real_research("Pasted: https://pasted.example.com/ref")

    assert set(fetched) == {"https://pasted.example.com/ref", "https://discovered.example.com/found"}
    assert len(results) == 2


def test_real_research_falls_back_to_pasted_urls_when_glasser_raises(monkeypatch):
    """discover_urls() itself is documented to never raise, but real_research
    guards against that contract ever being violated by a bug -- Glasser
    failing outright must never take down founder-pasted research with it."""
    def boom(idea_text):
        raise RuntimeError("Glasser blew up")

    monkeypatch.setattr(glasser_client, "discover_urls", boom)

    def fake_fetch(self, url):
        return Page(url=url, title="Pasted Page", text="pasted content")

    monkeypatch.setattr(research_adapter._web, "fetch", fake_fetch.__get__(research_adapter._web))

    results = research_adapter.real_research("See https://example.com/pasted for reference")
    assert len(results) == 1
    assert results[0]["source_url"] == "https://example.com/pasted"


def test_real_research_returns_empty_when_glasser_returns_nothing_and_no_pasted_urls(monkeypatch):
    monkeypatch.setattr(glasser_client, "discover_urls", lambda idea_text: [])
    assert research_adapter.real_research("An idea with no URLs at all") == []
