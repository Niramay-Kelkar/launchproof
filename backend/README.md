# LaunchProof backend (Person B starter)

Minimal FastAPI + SQLite vertical slice: run lifecycle, mocked research,
versioned experiment specs, hash-gated approval, a rendered landing page
with source_class-tagged events, and a report endpoint that only counts
real participant events as evidence. Already tested end-to-end.

## Run it

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

Visit http://127.0.0.1:8000/health — should return `{"status":"ok"}`.
Interactive API docs: http://127.0.0.1:8000/docs
**Founder workspace (real, wired to the API): http://127.0.0.1:8000/app/**

If port 8000 is already taken: `lsof -ti:8000 | xargs kill -9`, or run on another
port with `uvicorn main:app --reload --port 8001` (then edit `API_BASE` at the
top of `frontend/index.html`'s `<script>` if you change ports and don't serve
it through `/app` anymore).

If you ever see `sqlite3.OperationalError: attempt to write a readonly database`
on some synced/networked folder (Dropbox, some sandboxed VMs), point `DATABASE_URL`
in `.env` at a local path instead, e.g. `sqlite:////tmp/launchproof.db` — it's a
filesystem locking quirk, not an app bug.

### Local TLS trust store (only if real_research() can't fetch anything)

If `RESEARCH_PROVIDER_IS_REAL=true` and every fetch fails with
`FetchError: SSLCertVerificationError` even for an ordinary public site, your
local Python has an empty root CA store — check with:

```bash
python -c "import ssl; print(ssl.create_default_context().cert_store_stats())"
# x509_ca: 0  <- this is the problem
```

This is a known issue with the **python.org macOS installer** specifically
(its default `cert.pem` ships empty until certificates are installed) — it's
not a bug in `network.py`'s SSRF-safe fetcher or in this adapter, and `httpx`
calls (e.g. to Glasser) work anyway because `httpx` bundles `certifi`
directly regardless of the system trust store. Confirm you're on that
distribution with `python -c "import sys; print(sys.executable)"` — if the
path isn't under `/Library/Frameworks/Python.framework/...`, this specific
fix may not apply and the cause is something else.

Fix, smallest first:

```bash
# Option A (recommended, no sudo, scoped to how you run this project):
export SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())")
# then run uvicorn/pytest in that same shell. certifi is already installed
# (it's a transitive dependency of httpx, already in requirements.txt).

# Option B (system-wide fix for that Python installation, not project-scoped):
"/Applications/Python 3.13/Install Certificates.command"
```

Do not "fix" this by disabling certificate verification, bypassing the
pinned-TLS fetcher, or adding a fallback trust path in source code —
that would defeat the SSRF/MITM protections `network.py` is built around.

## Frontend: how it's wired

`frontend/index.html` is served by the backend itself at `/app` via
`StaticFiles`, so it's same-origin with the API and needs no CORS
configuration (CORS middleware is on anyway, in case anyone opens the file
directly during dev). It replaces the original mockup's fake in-memory state
with real `fetch()` calls against every endpoint below — open it, create a
run, and watch the actual database change.

Two behaviors worth knowing when you extend it:
- `/runs/{id}/landing?mode=preview` renders anytime an experiment spec exists
  and tags its events `qa` — safe to show before approval.
- `/runs/{id}/landing?mode=live` (the default) requires an approval and tags
  its events `participant` — this is the one real visitors would hit.

## The flow (same order the hackathon brief expects)

1. `POST /runs` `{"idea_text": "...", "owner": "A"}` -> `run_id`
2. `POST /runs/{run_id}/research` -> runs the (currently mocked) research adapter
3. `GET /runs/{run_id}/evidence` -> see what it found, with `is_mock` labeled honestly
4. `POST /runs/{run_id}/experiment` -> propose the experiment spec (see `schemas.ExperimentSpecCreate` for fields)
5. `GET /runs/{run_id}/approval-expected-hash?experiment_id=...` -> get the content hash the approval must match
6. `POST /runs/{run_id}/approve` -> approve (or reject) that exact version/hash
7. `GET /runs/{run_id}/landing` -> the rendered, approved landing page (fires its own events)
8. `POST /runs/{run_id}/events` -> record qa/demo/participant events manually if needed
9. `GET /runs/{run_id}/report` -> the Proof Pack: evidence, approvals, events by source_class, and a recommendation that refuses to call it "proceed" without real participant evidence

## What's deliberately cut for time (tell A and C)

- No Docker/Postgres — SQLite file, same persistence guarantee for the demo
- No Playwright suite yet — manually click through the flow above before demo
- `research_adapter.py` defaults to the labeled fixture adapter until you set
  `RESEARCH_PROVIDER_IS_REAL=true`; `real_research()` then fetches
  founder-pasted URLs plus, optionally, URLs discovered via Glasser (below)
- Glasser is the only real sponsor integration; everything else (including
  the merged LangGraph package in `launchproof/`) stays an honestly-labeled
  mock or an additive `/graph-runs/*` path, not part of `/runs/*`

## Glasser: the one real sponsor integration

`real_research()` in `research_adapter.py` builds its evidence from HTTPS
pages it can actually fetch, from two merged sources:

1. **Founder-pasted URLs** — any `https://` URL literally present in the
   idea text (unchanged from before Glasser existed).
2. **Glasser-discovered URLs** — candidate URLs found via
   [Glasser](https://glasser.ai)'s catalog/run API (`glasser_client.py`),
   gated independently of (1).

Both sources are merged, deduplicated, and capped together at one overall
limit (`research_adapter.MAX_URLS`, currently 5) before anything is fetched
— Glasser never gets its own separate budget on top of the founder's.

**Enabling it:**

```bash
RESEARCH_PROVIDER_IS_REAL=true   # required for real_research() to run at all
GLASSER_ENABLED=true
GLASSER_API_KEY=sk-...           # from your Glasser account; never commit this
```

Leave `GLASSER_ENABLED=false` (the default) or `GLASSER_API_KEY` blank to
keep research exactly as it is today — founder-pasted URLs only, no Glasser
calls made.

**What it does, step by step, per idea:**

1. `POST /v1/endpoints/search` — asks Glasser's catalog for a web-search
   Endpoint, biased toward Google web search by `query`/`use_case` text.
2. Picks the first catalog result whose name/description/tags mention
   "google" (preferring a `sync` endpoint over `async` if more than one
   matches). No match → Glasser is skipped for this idea.
3. `POST /v1/endpoints/inspect` on that endpoint — reads its `input_schema`,
   `sample_input` and current price *before* spending anything. If no
   query-like field can be identified in `sample_input`, Glasser is skipped
   rather than guessing at a provider-specific field name.
4. `POST /v1/runs` up to **3 times** (hardcoded, not configurable upward),
   each a small variation on the idea text ("", " competitors",
   " alternatives"), each with its own `idempotency-key` so an internal
   retry can never double-charge. Async runs (`202`) are polled via
   `GET /v1/runs/{id}` up to 5 times with a 1s interval.
5. Candidate URLs are harvested from each completed run's `output` —
   whatever shape the provider returns, since that's not a fixed contract
   across providers. A run's title/snippet is never treated as "read" —
   only a URL that the existing SSRF-safe fetcher (`launchproof/network.py`)
   successfully retrieves becomes an evidence item.

**Call budget per idea:** 1 search + 1 inspect + up to 3 run calls + up to
15 poll `GET`s (5 per async run) = at most ~20 HTTP calls to Glasser,
regardless of how large its catalog or a provider's result set is.

**When Glasser fails** (disabled, no key, no usable endpoint, provider
error, rate limited, timeout, insufficient balance, malformed response) it
logs a warning/info line and returns no URLs — `research_adapter.py` then
proceeds with founder-pasted URLs only, exactly like before Glasser existed.
It never falls back to inventing evidence, and fixture/mock behavior stays
governed entirely by `RESEARCH_PROVIDER_IS_REAL`, unaffected by Glasser.

**Known limitation:** the provider run ID and `charge_usd` Glasser reports
per run are logged (`logger.info`, in `glasser_client.py`), not persisted —
`EvidenceItem` has no provenance columns for this yet, and the project has
no migration tooling (`Base.metadata.create_all` only creates missing
tables, not missing columns on existing ones). Adding persisted
provider/charge provenance is a schema change for later, not done here.

## Things to hand to A and C immediately

- `schemas.py` and `models.py` are the actual data contracts — don't let anyone guess field names
- The approval gateway (`/runs/{run_id}/approve`) is the credibility centerpiece: it rejects a stale version or a tampered hash by design — show this live if a judge pushes on security
- `events_by_source_class` in the report is the other centerpiece: QA/demo clicks never inflate the "participant" count
