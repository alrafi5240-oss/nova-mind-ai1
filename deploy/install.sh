#!/usr/bin/env bash
# Install or update NOVA Cloud Agent on a fresh Ubuntu/Debian VPS.
#
#   git clone <this repo> nova-agent && cd nova-agent
#   sudo bash deploy/install.sh
#
# Re-running it pulls nothing by itself; run `git pull` first to update.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DEPLOY_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker"
  curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is missing; install docker-compose-plugin and re-run." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "==> First-time setup"
  read -rsp "Anthropic API key (input hidden): " api_key; echo
  [[ -n "$api_key" ]] || { echo "An API key is required." >&2; exit 1; }
  read -rp "Domain pointing at this server (leave empty to use http://<server-ip>): " domain
  token="$(openssl rand -hex 16)"
  umask 077
  cat > .env <<ENV
ANTHROPIC_API_KEY=$api_key
NOVA_AGENT_TOKEN=$token
NOVA_SITE=${domain:-:80}
NOVA_SANDBOX=docker
NOVA_DOCKER_IMAGE=python:3.11
NOVA_MAX_CONCURRENT=3
ENV
  echo "Saved settings to $DEPLOY_DIR/.env"
fi

mkdir -p /srv/nova
echo "==> Pulling the sandbox image"
docker pull "$(grep -E '^NOVA_DOCKER_IMAGE=' .env | cut -d= -f2- || echo python:3.11)"
echo "==> Building and starting NOVA"
docker compose up -d --build

site="$(grep -E '^NOVA_SITE=' .env | cut -d= -f2-)"
if [[ "$site" == ":80" || -z "$site" ]]; then
  url="http://$(curl -fsS -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
else
  url="https://$site"
fi
echo
echo "NOVA is running at: $url"
echo "Access token:       $(grep -E '^NOVA_AGENT_TOKEN=' .env | cut -d= -f2-)"
echo "Logs:               cd $DEPLOY_DIR && docker compose logs -f nova"
