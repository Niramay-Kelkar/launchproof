# LaunchProof

Evidence-first startup validation: a founder submits an idea, the app researches
it from real public sources, proposes an experiment, gates every risky action
behind an explicit founder approval, runs a real landing page, and refuses to
call anything "validated" without real participant evidence.

```
launchproof/
  backend/     FastAPI + SQLite app -- the primary demo path (/runs/*), with a
               merged LangGraph graph package (backend/launchproof/) available
               as an additive, parallel path (/graph-runs/*)
  frontend/    static founder workspace (index.html), served by the backend at /app
```

Start here: **[`backend/README.md`](backend/README.md)** — setup, how to run
it, the full `/runs/*` flow, the Glasser sponsor integration, and a known
local-TLS-trust-store note for macOS.
