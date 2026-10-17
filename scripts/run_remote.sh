#!/usr/bin/env bash
# run_remote.sh — rsync-based deploy from the workstation (deployment notes pattern: NO git
# clone on the VPS). Requires: ssh access as a sudo-capable user, rsync on both ends.
#
#   bash scripts/run_remote.sh setup    # rsync both repos + provision (idempotent)
#   bash scripts/run_remote.sh deploy   # rsync + reinstall deps + restart unit
#   bash scripts/run_remote.sh smoke    # real-key smoke on the server (≤1 RPD)
#   bash scripts/run_remote.sh status   # systemctl status + quota table
#   bash scripts/run_remote.sh logs     # tail daemon journal + app.log
#
# NOTE (0.10.0+): the systemd unit runs the RELEASED package via `uv tool`
# (~/.local/bin/gemini) — VERSION upgrades flow through push-CD (git tag →
# release.yml deploy job → deploy-tools). setup/deploy here sync SOURCE + config
# (config.yaml, scripts, prompts) and keep the tool binary at the latest release.
#
# Env: VPS_USER — SSH login user (default root: the VPS has NO 'agent' user — verified
# on live server 2026-10-08); APP_USER — service/venv owner (default = VPS_USER; set
# APP_USER=<user> later to run under a dedicated non-root user if one is created).
# VPS_HOST default YOUR_VPS_HOST. Non-standard SSH port/keys: via ~/.ssh/config for host.
set -euo pipefail

VPS_USER="${VPS_USER:-root}"
APP_USER="${APP_USER:-$VPS_USER}"
VPS_HOST="${VPS_HOST:-YOUR_VPS_HOST}"
DEST="$VPS_USER@$VPS_HOST"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"          # gemini repo
ROUTER_LOCAL="$(dirname "$HERE")/gemini-router"                  # sibling checkout
# NOTE: --rsync-path="sudo rsync" needs passwordless sudo for the login user (root: OK).
# The remote .env is NEVER touched (--exclude .env).
EXCL=(--exclude .git --exclude .venv --exclude __pycache__ --exclude logs
      --exclude '*.pyc' --exclude .env --exclude state.db --exclude .pytest_cache
      --exclude .ruff_cache)

[[ -d $ROUTER_LOCAL ]] || { echo "missing sibling repo: $ROUTER_LOCAL" >&2; exit 1; }
cmd="${1:-}"; [[ -n $cmd ]] || { sed -n '2,12p' "$0"; exit 2; }

sync_both() {
  echo "→ rsync gemini-router → /opt/apps/gemini-router"
  rsync -az --delete "${EXCL[@]}" --rsync-path="sudo rsync" -e ssh \
    "$ROUTER_LOCAL/" "$DEST:/opt/apps/gemini-router/"
  echo "→ rsync gemini → /opt/apps/gemini-mcp"
  rsync -az --delete "${EXCL[@]}" --rsync-path="sudo rsync" -e ssh \
    "$HERE/" "$DEST:/opt/apps/gemini-mcp/"
}

case "$cmd" in
  setup|deploy)
    sync_both
    ssh -t "$DEST" "sudo APP_USER=$APP_USER bash /opt/apps/gemini-mcp/scripts/setup_server.sh"
    ;;
  smoke)
    ssh -t "$DEST" "sudo -u $APP_USER /home/$APP_USER/.local/bin/gemini smoke"
    ;;
  status)
    ssh -t "$DEST" "systemctl --no-pager status gemini-mcp.service | head -8; \
      sudo -u $APP_USER env HOME=\$(getent passwd $APP_USER | cut -d: -f6) bash -c \
      'set -a; . /opt/apps/gemini-mcp/.env; set +a; cd /opt/apps/gemini-mcp && \
      \"\$HOME/.local/bin/gemini\" quota'"
    ;;
  logs)
    ssh -t "$DEST" "sudo journalctl -u gemini -n 40 --no-pager; \
      echo ---; tail -n 30 /opt/apps/gemini-mcp/logs/app.log 2>/dev/null || true"
    ;;
  *)
    echo "unknown command: $cmd" >&2; sed -n '2,12p' "$0"; exit 2
    ;;
esac
echo "done: $cmd"
