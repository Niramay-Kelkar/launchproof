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

## Not yet wired: QA, publish, and decision-memo steps

These exist in the schema/state machine only — no route in `main.py` ever
reaches or produces them, and we are **not** implementing them before the
demo. Don't assume they work; don't demo past `APPROVAL_REQUIRED` on the
`/runs/*` path expecting any of this to fire.

- **States** (`state_machine.py`): `QA_RUNNING`, `QA_PASSED`,
  `APPROVAL_REQUIRED_PUBLISH`, `PUBLISHING`, `PUBLISHED`, `MEASURING`,
  `DECISION_READY` are all valid *transitions* in `ALLOWED_TRANSITIONS`, but
  no route handler ever calls `set_status(db, run, ...)` with any of them.
  A run created via `/runs` cannot currently progress past
  `APPROVAL_REQUIRED` (or `ASSET_BUILT`, if someone manually sets that) on
  its own — someone would have to implement an asset-build step, a QA
  simulator, an auto-publish step, and a measurement window first.
  (`main.py`'s `landing_page()` does check `run.status` against a set
  containing several of these names, but checking membership is not the
  same as producing them — nothing ever sets a run's status to
  `"QA_RUNNING"` etc. in the first place.)
- **Tables** (`models.py`): `ActionReceipt` and `DecisionMemo` are defined
  (including `ActionReceipt.idempotency_key`, which looks ready for exactly
  this kind of use) but neither is ever queried, inserted into, or
  referenced by any route in `main.py` or `launchproof_routes.py`. They are
  schema only.

This doesn't affect `launchproof/`'s separate LangGraph workflow (`/graph-runs/*`,
also wired to Masky via `/api/runs` — see below), which has its own
automatic research → experiment → asset → report pipeline unrelated to this
state machine.

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

## Masky.AI integration (`/api/runs`)

Avery (Masky's COO persona) starts and polls a validation run through two
endpoints, built on top of the already-automated LangGraph package in
`launchproof/` (its `Runner`, normally reachable directly at `/graph-runs/*`)
rather than the manual `/runs/*` flow documented above.

**Why `launchproof/` and not `/runs/*`:** `/runs/*`'s status vocabulary
(`QA_RUNNING`, `PUBLISHING`, `MEASURING`, ...) matches a hackathon brief's
expectations more closely by name, but no code anywhere drives those
transitions automatically yet (`state_machine.py` defines them as valid, but
nothing calls `set_status(..., "QA_RUNNING")` etc.) — a human would have to
hand-author an `ExperimentSpecCreate` payload and manually operate every
step. `launchproof/`'s graph *does* run research → experiment → asset →
report automatically, pausing only at 4 founder-approval gates — it's the
only workflow in this repo that's actually wired end-to-end today.

**The honesty trade-off this implies:** `launchproof/`'s `ValidationReport`
is permanently simulated by design (`schemas.py`: `evidence_status:
"insufficient"`, `demand_established: False`, `real_customer_responses: 0`
— no real landing-page traffic is ever involved). So `GET /api/runs/{id}`
never returns real `visitors`/`signups`/`conversion_rate` — `metrics` is
always `null`, and `limitations` always says plainly that this used the
demo-mode simulated pipeline. A `"decision_ready"`/`"PROCEED"` result here
means "this concept is ready for a real experiment," never "demand is
proven." Statuses your brief lists that this graph has no equivalent
step for (`qa_running`, `qa_passed`, `publishing`, `published`, `measuring`)
are never returned — see `_UNSUPPORTED_BY_THIS_IMPLEMENTATION` in
`api_runs.py`.

### Endpoints

`POST /api/runs`

```json
{
  "idea_text": "An AI career assistant for international graduate students",
  "target_customer": "International graduate students",
  "requested_action": "Validate this startup idea"
}
```
→ `{"run_id": "lp_...", "status": "researching", "message": "Validation run started"}`

Validates `idea_text` (10–3000 chars, required), generates a unique `run_id`,
persists a `MaskyRun` row, and starts the graph in a FastAPI background task
— the request returns immediately. Send an `Idempotency-Key` header to make
retries safe: a repeated key returns the original `run_id` instead of
starting a second run.

`GET /api/runs/{run_id}` → current status, translated from the graph's real
state (`waiting_for_founder` + gate → `researched`/`experiment_proposed`/
`asset_built`/`approval_required`; `completed` → `decision_ready` or
`inconclusive`; `cancelled`/`revision_limit_reached` → `inconclusive`; a
background-task exception → `failed`). 404 if the run_id doesn't exist.

Founder approval at each of the 4 gates (needed before a run can reach
`decision_ready`) still goes through the existing `/graph-runs/{run_id}/
resume` endpoint — Masky only starts and reads runs, per "do not allow
publishing ... without a future approval gateway."

### Masky adapter (`masky_adapter.py`)

Masky is the *caller* of the two endpoints above, so no outbound Masky API
calls are required for the core flow. The adapter module exists for the
optional reverse direction — best-effort notifying Avery when a run reaches
`decision_ready`/`inconclusive`, instead of making Avery poll forever — plus
an inbound webhook receiver in case Masky ever pushes events to this backend.

```bash
MASKY_API_KEY=
MASKY_AVATAR_ID=
MASKY_WEBHOOK_SECRET=
MASKY_API_BASE_URL=https://api.masky.ai   # placeholder, unverified
PUBLIC_API_URL=                           # documentation only; not read by the app
```

- **No real Masky API contract was given or found**, so `RealMaskyProvider`
  is an interface-shaped best guess (endpoint path, payload shape), not a
  verified integration. It is never selected unless `MASKY_API_KEY` and
  `MASKY_AVATAR_ID` are both set.
- **`MockMaskyProvider` is the default** (and the only one actually
  exercised by the test suite): it logs what it would have sent and makes
  no network call. Every structured log line's `provider` field says
  `"mock"` or `"real_unverified"` so this is never ambiguous from the logs.
- `POST /webhooks/masky` verifies an inbound `X-Masky-Signature:
  sha256=<hex hmac>` against `MASKY_WEBHOOK_SECRET` and rejects (401) if it's
  missing, wrong, or the secret isn't configured. It logs and acknowledges
  receipt only — it does not act on the payload, since Masky's real webhook
  shape has not been confirmed.

### Logging

Every run/Masky action logs one JSON line via `masky_adapter.log_event()`:
`run_creation`, `workflow_start`, `agent_completion`, `workflow_failure`,
`final_recommendation`, `masky_notify_result`, `masky_webhook_received` —
each as `{"run_id", "source": "masky", "action", "timestamp", "status",
"provider", "error"}`. `main.py` calls `logging.basicConfig(level=INFO)` at
import time; without it these are silently dropped (Python's root logger
defaults to WARNING).

### Sample session

```bash
curl -X POST http://127.0.0.1:8000/api/runs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-key-1" \
  -d '{"idea_text": "An AI career assistant for international graduate students",
       "target_customer": "International graduate students",
       "requested_action": "Validate this startup idea"}'
# {"run_id":"lp_0ebd3b5fa156","status":"researching","message":"Validation run started"}

curl http://127.0.0.1:8000/api/runs/lp_0ebd3b5fa156
# {"run_id":"lp_0ebd3b5fa156","metrics":null,"evidence_type":null,"limitations":[],
#  "next_test":null,"recommendation":null,"summary":null,"status":"researched","gate":"research"}
```
(Output above is from an actual local run of this code, not hand-written.)

### Deploying for a real Masky configuration

Not done as part of this change — provisioning a host needs your own
account/billing decision. To deploy:

1. Pick a host that runs a long-lived container/process (Masky needs to
   reach a stable HTTPS URL): Render, Fly.io, or Railway are the simplest
   fits for a single `uvicorn` process + SQLite file.
2. Set `MASKY_API_KEY`, `MASKY_AVATAR_ID`, `MASKY_WEBHOOK_SECRET` as that
   host's secret/environment config — never commit them to `.env` (gitignored
   already) or anywhere in the repo.
3. Run `uvicorn main:app --host 0.0.0.0 --port $PORT` as the start command.
4. Give Masky's configuration the deployed base URL (also set locally as
   `PUBLIC_API_URL` for your own reference) plus `/api/runs` as the path to
   start a run and `/api/runs/{run_id}` to poll it.

### Known limitations

- `metrics`/`evidence_type: "real_participant_activity"` are never produced
  by this implementation — see the honesty trade-off above. Getting real
  visitor/signup numbers into this endpoint means wiring `/runs/*`'s
  landing-page+events pipeline instead, which itself needs the
  asset-build/QA/publish/measure automation that `state_machine.py` defines
  but no code implements yet.
- Mode is hardcoded to `"demo"` in `api_runs.py`; `"live"` mode (real
  OpenAI calls via `langchain-openai`) is not wired through the Masky
  adapter.
- `RealMaskyProvider` is unverified against Masky's actual API — treat it as
  a stub, not a working integration, until it's smoke-tested for real.
- The 4 founder-approval gates still require a human to call
  `/graph-runs/{run_id}/resume` directly; there is no Masky-facing approval
  path (by design — see the approval-gateway requirement above).

## Things to hand to A and C immediately

- `schemas.py` and `models.py` are the actual data contracts — don't let anyone guess field names
- The approval gateway (`/runs/{run_id}/approve`) is the credibility centerpiece: it rejects a stale version or a tampered hash by design — show this live if a judge pushes on security
- `events_by_source_class` in the report is the other centerpiece: QA/demo clicks never inflate the "participant" count
