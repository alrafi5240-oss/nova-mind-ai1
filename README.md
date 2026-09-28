# NOVA Cloud Agent

An autonomous coding agent in the style of Codex / Claude Code on the web. You
give it a task and (optionally) a Git repository; it clones the repo into an
isolated sandbox, reads the code, edits files, runs commands and tests, and
hands you back a diff you can review, follow up on, commit and push.

> **বাংলায়:** এটা একটা পূর্ণ cloud coding agent। আপনি একটা কাজ লিখবেন (আর চাইলে
> একটা GitHub repo URL দেবেন) — agent নিজে থেকে repo clone করে, আলাদা Docker
> sandbox-এ কোড পড়ে, ফাইল এডিট করে, টেস্ট চালায়, তারপর আপনাকে একটা diff দেখায়।
> আপনি follow-up মেসেজ দিয়ে পরিবর্তন চাইতে পারবেন, তারপর এক ক্লিকে commit/push।

## How it works

```
Browser UI ──REST/SSE──▶ FastAPI server ──▶ TaskManager (thread pool)
                                               │
                                               ├─ workspace: data/workspaces/<task>/  (git clone)
                                               ├─ sandbox:   Docker container, workspace at /workspace
                                               └─ Agent loop ◀──▶ Claude (Messages API, streaming)
                                                     tools: bash, str_replace_based_edit_tool
```

1. `POST /api/tasks` creates a task, clones the repo (or starts an empty git repo).
2. A sandbox container starts with the workspace bind-mounted and CPU/memory/PID limits.
3. The agent loop calls Claude with the Anthropic-defined **bash** and **text
   editor** tools. Each tool call runs in the sandbox; results go back to Claude.
   The loop runs until Claude ends its turn (or `NOVA_MAX_TURNS` is reached).
4. Every step (thinking summary, message, command, output) is logged as an event
   and streamed live to the UI over Server-Sent Events.
5. You review the diff, send follow-up messages (the same conversation continues),
   then commit to `nova/<task-id>` and optionally push.

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python -m nova_agent            # http://127.0.0.1:8787
```

Docker must be installed and running for the default sandbox. For local
development without Docker you can run commands directly on your machine —
**no isolation; only for trusted prompts on your own machine**:

```bash
NOVA_SANDBOX=local python -m nova_agent
```

### Run on your Mac (one command)

```bash
git clone https://github.com/alrafi5240-oss/nova-mind-ai1.git nova-agent
cd nova-agent
./run-mac.sh
```

The first run creates a Python environment in `.venv`, installs dependencies,
asks for your Anthropic API key (saved to `.env`, readable only by you) and
opens http://127.0.0.1:8787 in your browser. After that, `./run-mac.sh` starts
it straight away. It needs Python 3.10+ (`brew install python@3.12`) and git
(`xcode-select --install`). If Docker Desktop is running, each task gets an
isolated container; otherwise commands run directly on your Mac.

- `./run-mac.sh --phone` also serves NOVA on your Wi-Fi, protected by an
  access token. Click the phone icon at the bottom of the sidebar to copy a
  link that signs your phone in.
- `./run-mac.sh --port=9000` uses another port if 8787 is taken.

> **বাংলায়:** Mac-এর Terminal-এ উপরের ৩টা কমান্ড চালান। প্রথমবার API key চাইবে,
> তারপর ব্রাউজারে NOVA খুলে যাবে (http://127.0.0.1:8787)। ফোন থেকে দেখতে
> `./run-mac.sh --phone` চালান, তারপর sidebar-এর নিচের 📱 বোতাম থেকে লিংক কপি করে
> ফোনে খুলুন (ফোন আর Mac একই Wi-Fi-তে থাকতে হবে)।

### Deploy to a VPS (one command)

On a fresh Ubuntu/Debian server with ports 80/443 open:

```bash
git clone https://github.com/alrafi5240-oss/nova-mind-ai1.git nova-agent
cd nova-agent
sudo bash deploy/install.sh
```

The script installs Docker if needed, asks for your Anthropic API key and an
optional domain, generates an access token, and starts NOVA behind Caddy
(automatic HTTPS when you give a domain). It prints the URL and token at the
end. Open the URL on your phone and paste the token when asked. Settings live
in `deploy/.env`; update with `git pull && sudo bash deploy/install.sh`.

> **বাংলায়:** VPS-এ উপরের ৩টা কমান্ড চালান। API key আর (থাকলে) domain চাইবে,
> শেষে একটা URL আর token দেখাবে — মোবাইলে সেই URL খুলে token দিন।

### Running the server itself in Docker

Sandboxes are sibling containers created through the host's Docker daemon, so
the data directory must have the **same path** inside the server container and
on the host (the daemon resolves bind-mount paths on the host):

```bash
docker build -t nova-agent .
docker run -d -p 8787:8787 \
  -e ANTHROPIC_API_KEY -e NOVA_AGENT_TOKEN=change-me \
  -e NOVA_DATA_DIR=/srv/nova -v /srv/nova:/srv/nova \
  -v /var/run/docker.sock:/var/run/docker.sock \
  nova-agent
```

## Configuration

All settings are environment variables; see [`.env.example`](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Claude API key (required) |
| `NOVA_MODEL` | `claude-opus-5` | Model used by the agent |
| `NOVA_EFFORT` | `high` | `low` … `max`; `xhigh` is worth trying for hard coding tasks |
| `NOVA_SANDBOX` | `docker` | `docker` or `local` |
| `NOVA_DOCKER_IMAGE` | `python:3.11` | Image for task sandboxes (needs bash; git recommended) |
| `NOVA_DOCKER_NETWORK` | `bridge` | `none` blocks internet access from the sandbox |
| `NOVA_AGENT_TOKEN` | unset | If set, every API call needs `Authorization: Bearer <token>` |
| `NOVA_MAX_TURNS` | `80` | Upper bound on model calls per run |
| `NOVA_MAX_CONCURRENT` | `3` | Tasks that may run at the same time |

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/tasks` | `{"prompt", "repo_url"?, "branch"?}` – start a task |
| `GET` | `/api/tasks` | List tasks |
| `GET` | `/api/tasks/{id}` | Task with its event log |
| `GET` | `/api/tasks/{id}/events?after=N&follow=true` | SSE stream of events |
| `POST` | `/api/tasks/{id}/messages` | `{"message"}` – follow-up on a finished task |
| `POST` | `/api/tasks/{id}/cancel` | Stop a running task |
| `GET` | `/api/tasks/{id}/diff` | Current `git diff` of the workspace |
| `POST` | `/api/tasks/{id}/commit` | `{"message"?, "push"?}` – commit to `nova/<id>`, optionally push |
| `DELETE` | `/api/tasks/{id}` | Delete task and workspace |

## Security notes

- Commands the agent runs are untrusted model output. Use the Docker sandbox for
  anything beyond local experiments, and consider `NOVA_DOCKER_NETWORK=none`.
- The server's `ANTHROPIC_API_KEY` is never passed into sandboxes.
- File edits are confined to the task workspace (`..`, absolute paths and
  symlinks that escape it are rejected).
- Only `https://` repository URLs without embedded credentials are accepted (a
  token in the URL would land in `.git/config`, which the sandbox can read). For
  private repos, configure a Git credential helper for the server's user on the
  host: clone and push run on the host, and the sandbox never sees it.
- The server binds to `127.0.0.1` by default. Set `NOVA_AGENT_TOKEN` before
  exposing it on a network.

## Limitations

- The conversation history lives in memory: after a server restart, finished
  tasks keep their log and workspace, but can't take follow-up messages.
- One sandbox container per running task; it is removed when the run ends and
  recreated for a follow-up.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Tests use a scripted fake Claude client and the local sandbox, so they need
neither an API key nor Docker.
