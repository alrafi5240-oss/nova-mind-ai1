# CLAUDE.md

Guidance for AI assistants working in the `nova-mind-ai1` repository.

## What this repository is

**NOVA Cloud Agent** — an autonomous, Codex-style coding agent. A user submits a
task (plus an optional Git repo); the agent works in an isolated Docker sandbox
using Claude with the Anthropic-defined `bash` and `text_editor` tools, and
returns a reviewable diff. It is separate from the NOVA MIND chat product in
`alrafi5240-oss/nova-mind-ai`.

## Layout

- `nova_agent/config.py` — all settings, from `NOVA_*` env vars
- `nova_agent/sandbox.py` — `PersistentShell`, `DockerSandbox` (default), `LocalSandbox` (dev only)
- `nova_agent/tools.py` — tool declarations and handlers; path confinement lives in `ToolExecutor.resolve`
- `nova_agent/agent.py` — the agent loop (streaming Messages API, append-only transcript)
- `nova_agent/tasks.py` — task lifecycle, event log, persistence to `data/tasks/*.json`, diff/commit
- `nova_agent/server.py` — FastAPI REST + SSE API; serves `nova_agent/static/index.html`
- `deploy/` — production stack (docker compose + Caddy) and `install.sh` for a VPS
- `tests/` — pytest; `tests/fakes.py` provides a scripted fake Anthropic client

## Commands

```bash
pip install -r requirements-dev.txt
pytest                                   # no API key or Docker needed
NOVA_SANDBOX=local python -m nova_agent  # run without Docker (no isolation)
./run-mac.sh [--phone] [--port=N]        # user-facing local launcher (venv, .env, opens browser)
```

## Conventions

- Keep the transcript append-only: always append `response.content` unchanged
  (thinking blocks included); never edit earlier messages.
- Anything the model supplies (paths, commands, repo URLs) is untrusted: keep
  file access inside the workspace and never pass server credentials into a sandbox.
- The UI is a single dependency-free HTML file; build DOM with `textContent`, not
  `innerHTML`, for any task or model data.
- Commit to the assigned feature branch, not `main`.
