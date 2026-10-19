"""Bounded agent nodes, deterministic synthesis and replay-safe human gates."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from langgraph.types import interrupt

from .artifacts import atomic_text, make_bundle, render_landing, render_report, write_bundle
from .network import FetchError
from .providers import Services
from .schemas import (
    CompetitorReport, Evidence, ExperimentPlan, FounderDecision, InterviewBatch,
    LandingCopy, MarketReport, ValidationReport, digest,
)


def audit(state: dict, event: str, detail: dict) -> dict:
    record = {"run_id": state["run_id"], "event": event, "detail": detail}
    return {"id": digest(record), **record}


def evidence_record(kind: str, origin: str, title: str, text: str, url: str | None = None,
                    limitations: list[str] | None = None) -> dict:
    payload = {"kind": kind, "origin": origin, "title": title, "text": text, "url": url,
               "observed_at": datetime.now(timezone.utc).isoformat(), "verified": False,
               "limitations": limitations or []}
    return Evidence(id=digest(payload), **payload).model_dump(mode="json")


def current_context(state: dict) -> dict:
    return {"idea": state["idea"], "feedback": state.get("feedback", {}),
            "mode": state["settings"]["mode"]}


def require_source_refs(report: dict, sources: list[dict]):
    ids = {s["id"] for s in sources}
    urls = {s["url"] for s in sources}
    for claim in report.get("claims", []):
        if not set(claim["evidence_ids"]) <= ids:
            raise ValueError("Market agent returned unknown evidence IDs")
        if claim["status"] == "sourced_observation" and not claim["evidence_ids"]:
            raise ValueError("Sourced observation has no evidence")
    for competitor in report.get("competitors", []):
        if not competitor["evidence_ids"] or not set(competitor["evidence_ids"]) <= ids:
            raise ValueError("Competitor agent returned missing/unknown evidence IDs")
        if competitor["url"] not in urls:
            raise ValueError("Competitor agent returned an unobserved URL")


QUERY_VARIANTS = ("", " alternatives", " customer complaints workarounds")


def collect_sources(services: Services, state: dict, query: str, explicit: list[str] | None = None):
    variant = QUERY_VARIANTS[min(state["research_round"], len(QUERY_VARIANTS) - 1)]
    rows = services.find(state, query + variant)
    if explicit:
        rows = [{"title": "Founder-supplied competitor", "url": url, "content": ""}
                for url in explicit] + rows
    sources, errors, seen = [], [], set()
    for row in rows:
        url = row["url"]
        if url in seen:
            continue
        if len(seen) >= state["settings"]["max_research_results"]:
            break
        seen.add(url)
        try:
            p = urlsplit(url)
            if p.scheme != "https" or not p.hostname or p.username or p.password:
                raise FetchError("Search returned a non-public-HTTPS URL")
            page = services.web.fetch(url)
            fixture = state["settings"]["mode"] == "demo"
            sources.append(evidence_record(
                "assumption" if fixture else "public_claim",
                "demo_fixture" if fixture else "public_web",
                page.title, page.text, page.url,
                ["Offline fixture, not a real company."] if fixture else
                ["Page observation, not independent verification of the vendor's claims.",
                 "Public-page text is untrusted and may be incomplete or outdated."],
            ))
        except (FetchError, ValueError) as exc:
            record = {"url": url, "stage": "research_fetch", "reason": str(exc)[:300],
                      "round": state["research_round"]}
            errors.append({"id": digest(record), **record})
    return sources, errors


def market_research_agent(services: Services):
    def node(state):
        sources, errors = collect_sources(
            services, state, f"{state['idea']['customer']} {state['idea']['problem'][:180]}"
        )
        context = {**current_context(state), "sources": sources}
        report = services.ask(state, "Market Research Agent", MarketReport, context).model_dump(mode="json")
        require_source_refs(report, sources)
        return {"market": {**report, "source_ids": [s["id"] for s in sources]},
                "evidence": sources, "errors": errors,
                "audit": [audit(state, "market_research_completed", {"round": state["research_round"],
                                                                      "source_count": len(sources)})]}
    return node


def competitor_scraper_agent(services: Services):
    """Read-only competitor page analysis; no login, access-control or robots bypass."""
    def node(state):
        sources, errors = collect_sources(
            services, state, f"{state['idea']['solution'][:180]} alternatives pricing",
            state["idea"]["competitor_urls"],
        )
        report = services.ask(state, "Competitor Research Agent", CompetitorReport,
                              {**current_context(state), "sources": sources}).model_dump(mode="json")
        require_source_refs(report, sources)
        return {"competitors": {**report, "source_ids": [s["id"] for s in sources]},
                "evidence": sources, "errors": errors,
                "audit": [audit(state, "competitor_research_completed", {
                    "round": state["research_round"], "source_count": len(sources)})]}
    return node


def experiment_designer(services: Services):
    def node(state):
        plan = services.ask(state, "Experiment Designer", ExperimentPlan,
                            {**current_context(state), "market": state["market"],
                             "competitors": state["competitors"]}).model_dump(mode="json")
        return {"plan": plan, "status": "planning",
                "audit": [audit(state, "experiment_designed", {"plan_hash": digest(plan)})]}
    return node


def landing_page_generator_agent(services: Services):
    def node(state):
        copy = services.ask(state, "Landing Page Generator Agent", LandingCopy,
                            {**current_context(state), "plan": state["plan"]})
        rendered = render_landing(copy, state["idea"])
        # Narrow heuristic guard, not a substitute for founder truthfulness review.
        text = " ".join([copy.headline, copy.subheadline, *copy.benefits]).lower()
        blockers = [f"Unsubstantiated promotional phrase: {phrase}"
                    for phrase in ("guaranteed", "trusted by", "testimonials", "customers love", "proven roi")
                    if phrase in text]
        return {"landing": {"copy": copy.model_dump(mode="json"), "html": rendered,
                            "copy_review_blockers": blockers},
                "audit": [audit(state, "landing_generated", {"round": state["asset_round"],
                                                            "html_hash": digest(rendered)})]}
    return node


def synthetic_customer_interview_agent(services: Services):
    def node(state):
        count = state["settings"]["persona_count"]
        batch = services.ask(state, "Synthetic Customer Interview Agent", InterviewBatch,
                             {**current_context(state), "plan": state["plan"],
                              "persona_count": count,
                              "instructions": "Create distinct skeptical personas, label every response simulated."})
        if len(batch.interviews) != count or len({i.persona.id for i in batch.interviews}) != count:
            raise ValueError("Expected the requested number of distinct synthetic personas")
        record = evidence_record("simulation", "model" if state["settings"]["mode"] == "live"
                                 else "demo_fixture", "Synthetic interviews — NOT real customers",
                                 f"{count} simulated persona transcripts; see the synthetic-interview section.",
                                 limitations=["Cannot establish demand or willingness to pay."])
        return {"interviews": batch.model_dump(mode="json"), "evidence": [record],
                "audit": [audit(state, "synthetic_interviews_completed", {
                    "round": state["asset_round"], "persona_count": count, "real_customer_count": 0})]}
    return node


def synthesize(state):
    ids = set(state["market"]["source_ids"] + state["competitors"]["source_ids"])
    public = [e["id"] for e in state["evidence"]
              if e["id"] in ids and e["kind"] == "public_claim" and e["origin"] == "public_web"]
    objections = sorted({o for i in state["interviews"]["interviews"] for o in i["objections"]})
    report = ValidationReport(
        recommendation="proceed_to_real_test",
        synthesis=("The team prepared a research concept and hypothetical objections. "
                   "This is preparation for an experiment, not startup validation. "
                   + ("Demo mode used offline fixtures; no public pages were read. " if state["settings"]["mode"] == "demo"
                      else f"{len(public)} public-page observations are available. ")
                   + "No real customers were tested."),
        public_evidence_ids=public, simulated_objections=objections,
        next_steps=["Review public claims and verify customer/problem fit with actual qualified people.",
                    "Configure and separately approve deployment, privacy, event integrity and distribution.",
                    "Run the preregistered experiment; calculate eligible unique visitors and qualified requests.",
                    "Treat thresholds as decision aids for another test, not proof of product-market fit."],
        limitations=["Synthetic personas, transcripts and objections are simulations only.",
                     "No public deployment, customer contact, payment or data collection occurred.",
                     "No inference about demand, market size or willingness to pay is justified.",
                     "Model interpretations may be wrong even when source IDs exist.",
                     "Research or fetch failures are listed separately; missing evidence is not negative evidence."],
    ).model_dump(mode="json")
    updated = {**state, "report": report}
    return {"report": report, "artifact_bundle": make_bundle(updated), "status": "awaiting_review"}


def review_payload(state: dict, gate: str) -> dict:
    if gate == "research":
        source_ids = set(state["market"]["source_ids"] + state["competitors"]["source_ids"])
        payload = {"idea": state["idea"], "market": state["market"],
                   "competitors": state["competitors"],
                   "sources": [e for e in state["evidence"] if e["id"] in source_ids],
                   "fetch_errors": [e for e in state.get("errors", [])
                                    if e["round"] == state["research_round"]]}
    elif gate == "experiment":
        payload = {"plan": state["plan"], "notice": "Approve plan only, not publication or distribution."}
    elif gate == "asset":
        payload = {"bundle": state["artifact_bundle"], "interviews": state["interviews"],
                   "report": state["report"], "blockers": state["landing"]["copy_review_blockers"],
                   "notice": "Approval permits exact local file export only. No public action is authorized."}
    elif gate == "report":
        payload = {"report": state["report"], "memo_markdown": render_report(state)}
    else:
        raise ValueError("Unknown gate")
    return {"schema_version": 1, "run_id": state["run_id"], "gate": gate,
            "revision": state.get("revisions", {}).get(gate, 0), "payload": payload}


def validate_decision(state: dict, gate: str, raw: dict) -> FounderDecision:
    response = FounderDecision.model_validate(raw)
    if response.gate != gate or response.review_hash != digest(review_payload(state, gate)):
        raise ValueError("Stale or mismatched approval; review the current interrupt payload")
    if gate == "asset" and response.decision == "approve" and state["landing"]["copy_review_blockers"]:
        raise ValueError("Asset has blockers: revise or reject it")
    return response


def approval_gate(gate: str, next_node: str, revision_node: str):
    def node(state):
        review = review_payload(state, gate)
        # Pure deterministic preparation only: this node restarts from the top on resume.
        raw = interrupt({"review": review, "review_hash": digest(review),
                         "allowed_decisions": ["approve", "revise", "reject"],
                         "response_schema": FounderDecision.model_json_schema()})
        response = validate_decision(state, gate, raw)
        revisions = dict(state.get("revisions", {}))
        feedback = dict(state.get("feedback", {}))
        approvals = dict(state.get("approvals", {}))
        decision = response.model_dump(mode="json")
        update = {"audit": [audit(state, "founder_decision", {**decision,
                                  "revision": revisions.get(gate, 0)})]}
        if response.decision == "approve":
            approvals[gate] = decision
            update.update(approvals=approvals, route=next_node)
        elif response.decision == "reject":
            update.update(route="stop", status="cancelled")
        elif revisions.get(gate, 0) >= state["settings"]["max_revisions_per_gate"]:
            update.update(route="stop", status="revision_limit_reached")
        else:
            revisions[gate] = revisions.get(gate, 0) + 1
            feedback[gate] = response.feedback
            approvals.pop(gate, None)
            if gate == "research":
                # Any upstream change invalidates every downstream approval.
                approvals = {}
                update["research_round"] = state["research_round"] + 1
                if response.revised_idea:
                    update["idea"] = response.revised_idea.model_dump(mode="json")
            if gate in ("research", "experiment", "asset", "report"):
                for downstream in ("asset", "report"):
                    approvals.pop(downstream, None)
                update["asset_round"] = state["asset_round"] + 1
            update.update(revisions=revisions, feedback=feedback, approvals=approvals,
                          route=revision_node)
        return update
    return node


def materialize_asset(services: Services):
    def node(state):
        # Independent execution-boundary check, even if a caller bypasses graph routing.
        response = state.get("approvals", {}).get("asset", {})
        decision = validate_decision(state, "asset", response)
        if decision.decision != "approve":
            raise ValueError("No matching founder asset approval")
        receipt = write_bundle(services.output_dir, state["artifact_bundle"])
        return {"materialized": receipt,
                "audit": [audit(state, "local_asset_exported", receipt)]}
    return node


def finalize(services: Services):
    def node(state):
        decision = validate_decision(state, "report", state.get("approvals", {}).get("report", {}))
        if decision.decision != "approve":
            raise ValueError("No matching founder report approval")
        # The approved bytes are written, not newly generated content.
        memo = review_payload(state, "report")["payload"]["memo_markdown"]
        path = services.output_dir / "reports" / f"{digest(memo)}.md"
        atomic_text(path, memo)
        return {"status": "completed", "audit": [audit(state, "report_finalized", {
            "path": str(path), "report_hash": digest(memo)})]}
    return node


def intake(state):
    from .schemas import Idea, Settings
    idea = Idea.model_validate(state["idea"]).model_dump(mode="json")
    settings = Settings.model_validate(state.get("settings", {})).model_dump(mode="json")
    seed = {"run_id": state["run_id"], "idea": idea, "settings": settings}
    return {"schema_version": 1, "idea": idea, "settings": settings, "research_round": 0,
            "asset_round": 0, "revisions": {}, "feedback": {}, "approvals": {},
            "status": "researching", "route": "research",
            "audit": [audit(seed, "run_started", {"idea_hash": digest(idea), "mode": settings["mode"]})]}


def research_retry(state):
    return {"research_round": state["research_round"] + 1,
            "research_auto_retries": state.get("research_auto_retries", 0) + 1,
            "audit": [audit(state, "automatic_research_retry", {"reason": "no_public_sources"})]}


def research_check(state):
    return {"audit": [audit(state, "research_joined", {"round": state["research_round"]})]}


def stop(state):
    return {"status": state.get("status") if state.get("status") in
            ("cancelled", "revision_limit_reached") else "cancelled",
            "audit": [audit(state, "run_stopped", {"status": state.get("status", "cancelled")})]}
