#!/usr/bin/env bash
# Run NOVA on your Mac (also works on Linux).
#
#   ./run-mac.sh            open NOVA at http://127.0.0.1:8787 on this Mac
#   ./run-mac.sh --phone    also allow phones on the same Wi-Fi (creates an access token)
#   ./run-mac.sh --port=9000
#
# The first run creates .venv, installs dependencies and asks for your
# Anthropic API key (saved to .env, readable only by you).
set -euo pipefail
cd "$(dirname "$0")"

PORT="${NOVA_PORT:-8787}"
PHONE=0
for arg in "$@"; do
  case "$arg" in
    --phone) PHONE=1 ;;
    --port=*) PORT="${arg#*=}" ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 1 ;;
  esac
done

say() { printf '\033[1;38;5;173m==>\033[0m %s\n' "$*"; }

# 1. Python 3.10+ and git
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [[ -z "$PY" ]]; then
  echo "NOVA needs Python 3.10 or newer (macOS ships 3.9)."
  echo "Install it with Homebrew (https://brew.sh):  brew install python@3.12"
  exit 1
fi
if ! command -v git >/dev/null 2>&1; then
  echo "git is missing. Install Apple's command line tools:  xcode-select --install"
  exit 1
fi

# 2. Virtual environment and dependencies (reinstalled only when requirements change)
if [[ ! -x .venv/bin/python ]]; then
  say "Creating a Python environment in .venv"
  "$PY" -m venv .venv
fi
req_sum="$(.venv/bin/python -c 'import hashlib; print(hashlib.sha256(open("requirements.txt","rb").read()).hexdigest())')"
if [[ "$(cat .venv/.requirements.sha 2>/dev/null)" != "$req_sum" ]]; then
  say "Installing dependencies (first run takes a minute)"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
  echo "$req_sum" > .venv/.requirements.sha
fi

# 3. Settings in .env
touch .env && chmod 600 .env
env_has() { grep -Eq "^$1=.+" .env; }
if ! env_has ANTHROPIC_API_KEY && [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  read -rsp "Paste your Anthropic API key (input hidden): " key; echo
  [[ -n "$key" ]] || { echo "An API key is required." >&2; exit 1; }
  echo "ANTHROPIC_API_KEY=$key" >> .env
  say "Saved the key to .env"
fi

# Docker Desktop gives every task an isolated container; without it, commands
# run directly on this Mac.
if ! env_has NOVA_SANDBOX && [[ -z "${NOVA_SANDBOX:-}" ]]; then
  if docker info >/dev/null 2>&1; then
    export NOVA_SANDBOX=docker
  else
    export NOVA_SANDBOX=local
    say "Docker Desktop is not running: NOVA will run commands directly on this Mac."
    echo "    Only give it tasks you trust, or start Docker Desktop and re-run for isolation."
  fi
fi

HOST=127.0.0.1
if [[ $PHONE -eq 1 ]]; then
  HOST=0.0.0.0
  if ! env_has NOVA_AGENT_TOKEN && [[ -z "${NOVA_AGENT_TOKEN:-}" ]]; then
    echo "NOVA_AGENT_TOKEN=$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(12))')" >> .env
  fi
  token="${NOVA_AGENT_TOKEN:-$(grep -E '^NOVA_AGENT_TOKEN=' .env | tail -1 | cut -d= -f2-)}"
  say "Phone access is on. Access token: $token"
  echo "    macOS may ask to allow incoming connections for Python: click Allow."
fi

# 4. Start
exec .venv/bin/python -m nova_agent --host "$HOST" --port "$PORT" --open
