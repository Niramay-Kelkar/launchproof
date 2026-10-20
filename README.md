# LaunchProof

**Evidence-first startup idea validation.** A founder submits an idea, LaunchProof
researches it from real public sources (never invented), proposes a testable
experiment, gates every risky or costly action behind an explicit founder
approval, runs a real preview landing page, and refuses to call anything
"validated" without real participant evidence. QA clicks and demo clicks never
count as demand — only a labeled `participant` event does.

It does not prove demand. It prepares a validation experiment, honestly.

## Try it in 60 seconds

```bash
git clone https://github.com/Niramay-Kelkar/launchproof
cd launchproof/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

Open **http://127.0.0.1:8000/app/** — that's the real founder workspace, wired
to the live API, not a mockup. No API keys needed for this path: research runs
in clearly-labeled fixture mode out of the box.

## What's actually in this repo

```
launchproof/
  backend/
    main.py                FastAPI app — the primary demo path (/runs/*)
    launchproof_routes.py  additive /graph-runs/* path (see below)
    api_runs.py            /api/runs — Masky.AI integration, built on /graph-runs
    launchproof/           merged LangGraph workflow package (its own graph, gates, tests)
    research_adapter.py    real research: founder-pasted + Glasser-discovered URLs,
                            fetched through an SSRF-safe, pinned-TLS fetcher
    glasser_client.py      the one real, live sponsor integration
    masky_adapter.py       contract-verified against Masky's real docs, not yet
                            exercised against a live Masky call
  frontend/
    index.html             the founder workspace, served by the backend at /app
```

Three API surfaces exist on purpose, not by accident:

| Path | What it is | Status |
|---|---|---|
| `/runs/*` | Hand-authored FastAPI + SQLite state machine, hash-gated approval | **Primary demo path.** Fully working, fully tested. |
| `/graph-runs/*` | A merged LangGraph package — 4 founder-approval gates, durable checkpointing, SSRF-safe research agents | Additive. Runs automatically end-to-end, pausing only at approval gates. |
| `/api/runs` | A thin layer for Masky.AI's COO-persona avatar to start/poll a run, built on top of `/graph-runs` | Request/response contract live and tested; outbound Masky calls default to a logged mock. |

## The demo flow (`/runs/*`)

1. `POST /runs` — submit an idea
2. `POST /runs/{id}/research` — researches it (fixture, or real via Glasser — see below)
3. `GET /runs/{id}/evidence` — every item honestly labeled `is_mock` true/false, and now tagged `source: founder_pasted` or `glasser`
4. `POST /runs/{id}/experiment` — propose the experiment spec
5. `GET /runs/{id}/approval-expected-hash` → `POST /runs/{id}/approve` — approval is bound to the *exact* content hash of that spec version; a stale or tampered version is rejected
6. `GET /runs/{id}/landing` — the real rendered preview page (no form, no tracking script beyond labeled event pings, never claims an existing product)
7. `POST /runs/{id}/events` — `qa` / `demo` / `participant`, tagged at the source
8. `GET /runs/{id}/report` — the Proof Pack: evidence, approvals, events by class, and a recommendation that stays `inconclusive` until real participant evidence exists

Full technical detail, every endpoint, every env var: **[`backend/README.md`](backend/README.md)**.

## What's real, what's mocked — stated plainly

- **Glasser (research discovery) — the one live sponsor integration.** Real
  catalog search → inspect → bounded run calls, real competitor URLs returned,
  fetched through the same SSRF-safe reader the LangGraph package uses. Verified
  end to end against Glasser's real API. Disabled by default (`GLASSER_ENABLED=false`);
  when it fails or is off, research falls back to founder-pasted URLs only — it
  never invents evidence.
- **Masky.AI (avatar notification) — contract-verified, not yet live-called.**
  The request/response shapes in `masky_adapter.py` were checked directly
  against Masky's published docs (not guessed), including dropping an earlier
  invented webhook once the docs confirmed Masky only polls. No real call has
  been made to Masky's API — the default provider is a logged mock, and the
  code says so explicitly in its own logs (`provider: "mock"` vs
  `"real_unverified"`).
- **The LangGraph workflow (`/graph-runs/*`) — real automation, permanently
  simulated evidence.** It runs research → experiment design → asset build →
  report automatically, pausing only at 4 founder-approval gates. Its
  synthetic customer interviews are explicitly labeled simulations, and its
  final report hard-codes `demand_established: false` — it was built to never
  overstate what it found.
- **The `/runs/*` lifecycle beyond approval (QA, publish, measure) — schema
  only, not implemented.** `state_machine.py` defines those transitions and
  `models.py` defines tables for them (`ActionReceipt`, `DecisionMemo`), but no
  route ever reaches them. Documented honestly in `backend/README.md` rather
  than left to be discovered mid-demo.

## Safety properties, each one testable live

- Approval is cryptographically bound to the exact experiment version shown to
  the founder — resubmit a stale hash and the backend rejects it (`409`, not a
  silent no-op).
- `events_by_source_class` means a QA click or a demo click can never inflate
  the participant count the final recommendation is based on.
- All real web fetching (both Glasser-discovered and founder-pasted URLs) goes
  through one pinned-TLS, private-IP-blocking, robots.txt-respecting reader —
  there is no second, less careful fetch path anywhere in the codebase.
- A Masky notification is claimed with a single atomic SQL `UPDATE`, not a
  read-then-write check — verified under genuine concurrent load with
  `threading.Barrier`, not timing guesses, so a race can't trigger a duplicate
  paid call.
- Nothing in this codebase ever disables TLS verification, bypasses the SSRF
  guard, or logs a secret (API keys, auth headers) to get a feature working
  faster.

## Running the tests

```bash
cd launchproof      # repo root
pytest
```

One command, both suites: 93 passed (`backend/tests/` — the FastAPI app,
research adapter, Glasser client, Masky adapter, migrations; plus
`backend/launchproof/tests/` — the LangGraph package's own suite). No test
makes a real network call; every external provider is mocked at the transport
layer.

## Known limitations

- Real participant metrics (`visitors`, `signups`, `conversion_rate`) are
  never produced by `/api/runs`, by design — getting real numbers requires the
  asset-build → QA → publish → measure automation that's defined in schema but
  not yet implemented (see above).
- `RealMaskyProvider` matches Masky's documented contract on paper; it has not
  been exercised against a live call.
- Glasser's provider run ID and reported charge are logged but not persisted
  to the evidence record yet.
- SQLite only, by design for a hackathon timeline — swapping in Postgres for
  production is a documented, deliberate next step, not an oversight
  (`backend/launchproof/README.md` has the production-swap notes for the
  LangGraph checkpointer specifically).

## Credits

Built across three workstreams: the FastAPI backend and state machine, the
LangGraph research/approval/asset workflow, and the sponsor integrations
(Glasser, Masky) layered on top without touching either core path.
