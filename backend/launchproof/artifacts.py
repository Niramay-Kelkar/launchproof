"""Constrained local preview generation; no deployment, outreach, spend or PII forms."""
from __future__ import annotations

import hashlib
import html
import json
import os
import tempfile
from pathlib import Path

from .schemas import LandingCopy, digest


def render_landing(copy: LandingCopy, idea: dict) -> str:
    e = html.escape
    benefits = "".join(f"<li>{e(item)}</li>" for item in copy.benefits)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{e(idea['name'])} — concept preview</title>
<style>
:root{{--text-xs:clamp(.75rem,.7rem + .25vw,.875rem);--text-base:clamp(1rem,.95rem + .25vw,1.125rem);
--text-hero:clamp(2rem,1.2rem + 2.5vw,3.5rem);--font-body:'Aptos','Segoe UI',sans-serif}}
*{{box-sizing:border-box}}body{{margin:0;background:#f3f1e9;color:#17251e;font-family:var(--font-body);font-size:var(--text-base);line-height:1.65}}
main{{max-width:960px;margin:auto;padding:clamp(24px,7vw,88px)}}header{{border-top:3px solid #244b35;padding-top:20px}}
.label{{font-size:var(--text-xs);letter-spacing:.06em;text-transform:uppercase;color:#39513f}}
h1{{font-size:var(--text-hero);line-height:1.08;max-width:20ch;letter-spacing:-.035em;margin:42px 0 24px;overflow-wrap:anywhere}}
p{{max-width:62ch}}ul{{padding-left:24px;margin:32px 0}}li{{padding:6px 0}}a{{display:inline-block;background:#244b35;color:#fff;padding:14px 22px;border-radius:4px;text-decoration:none;font-weight:650}}
a:focus-visible{{outline:3px solid #8b491e;outline-offset:4px}}section{{border-top:1px solid #a8b3a8;margin-top:56px;padding-top:20px}}
footer{{margin-top:48px;font-size:var(--text-xs);color:#405345}}@media(max-width:420px){{main{{padding:24px}}}}
</style></head><body><main><header>{e(idea['name'])} / Private concept preview</header>
<p class="label">{e(copy.availability)}</p><h1>{e(copy.headline)}</h1><p>{e(copy.subheadline)}</p>
<ul>{benefits}</ul><a href="#pilot">{e(copy.cta_label)}</a>
<section id="pilot"><h2>Proposed pilot</h2><p>This page explores an idea, not an available product.
No sign-up or payment is being collected. A founder must configure and approve a separate live experiment before inviting participants.</p></section>
<footer>LaunchProof concept preview. No analytics, external scripts, cookies or personal-data forms.
Synthetic reviews are internal simulations, not customer evidence.</footer></main></body></html>'''


def render_report(state: dict) -> str:
    report = state["report"]
    lines = [f"# LaunchProof: {state['idea']['name']}", "",
             f"Mode: {state['settings']['mode']}", "",
             f"Recommendation: {report['recommendation']}",
             "Evidence status: insufficient. Demand established: no. Real customer responses: 0.", "",
             report["synthesis"], "", "## Next steps"]
    lines += [f"- {item}" for item in report["next_steps"]]
    lines += ["", "## Limitations"] + [f"- {item}" for item in report["limitations"]]
    lines += ["", "## Evidence ledger"]
    for row in state.get("evidence", []):
        label = row["title"].replace("[", "\\[").replace("]", "\\]")
        url = row.get("url")
        citation = f"[{label}](<{url}>)" if url else label
        lines += [f"- {row['id']} / {row['kind']} / {row['origin']}: {citation}",
                  f"  Observed: {row['observed_at']}. {row['text'][:1200]}"]
    lines += ["", "## Synthetic interviews", "", "SIMULATION ONLY — NOT CUSTOMER DEMAND."]
    for interview in state["interviews"]["interviews"]:
        lines += ["", f"### {interview['persona']['role']}"] + interview["transcript"]
    return "\n".join(lines) + "\n"


def make_bundle(state: dict) -> dict:
    landing = state["landing"]
    content = landing["html"]
    return {
        "action": "write_local_preview",
        "run_id": state["run_id"],
        "asset_round": state["asset_round"],
        "files": {"index.html": content,
                  "experiment.json": json.dumps(state["plan"], indent=2, ensure_ascii=False) + "\n"},
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "publication": "none", "distribution": "none", "spend_usd": 0,
        "data_collection": [],
    }


def atomic_text(path: Path, text: str):
    """Atomic replace with content-addressed directory makes replay harmless."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".launchproof-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_bundle(root: Path, bundle: dict) -> dict:
    key = digest(bundle)
    destination = root / key
    for filename, content in bundle["files"].items():
        if filename not in {"index.html", "experiment.json"}:
            raise ValueError("Unexpected artifact filename")
        atomic_text(destination / filename, content)
    atomic_text(destination / "manifest.json", json.dumps({
        "bundle_hash": key, "action": bundle["action"], "run_id": bundle["run_id"],
        "content_sha256": bundle["content_sha256"], "publication": "none",
    }, indent=2) + "\n")
    return {"path": str(destination), "bundle_hash": key, "publication": "none"}
