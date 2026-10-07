#!/usr/bin/env bash
# setup_server.sh — idempotent provisioning of gemini-mcp on the VPS. Run AS ROOT on the
# server (run_remote.sh does this over ssh). Server-spec compliant:
#   * never touches ufw/iptables, external-service, unrelated-service, sysctl, ports 22/443/20724;
#   * adds ONLY a loopback daemon 127.0.0.1:8790 + unit with MemoryMax=350M;
#   * uv-managed venv under /opt/apps (PEP 668: no global pip installs).
# Re-running is safe: dirs/venv/.env persist, deps are reinstalled, unit is restarted.
set -euo pipefail

APP=/opt/apps/gemini-mcp
ROUTER_SRC=/opt/apps/gemini-router          # rsync'd source (no GitHub auth on server)
SHARED=/opt/apps/shared/gemini-router       # shared quota ledger (MCP + future tldr)
APP_USER="${APP_USER:-root}"   # VPS has no dedicated user yet (verified 2026-10-08); service runs as root with unit hardening
ENVF="$APP/.env"
EXTERNAL_ENV=/opt/apps/external-consumer/.env

command -v uv >/dev/null 2>&1 || {
  echo "uv not found. Install as root: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1; }
id "$APP_USER" >/dev/null 2>&1 || { echo "user '$APP_USER' missing (override: APP_USER=...)" >&2; exit 1; }

mkdir -p "$APP/logs" "$SHARED" "$ROUTER_SRC"
chown -R "$APP_USER:$APP_USER" "$APP" "$SHARED" "$ROUTER_SRC"
chmod 770 "$SHARED"

# ---- .env: created once, never overwritten (secrets stay on the server) ----
if [[ ! -f $ENVF ]]; then
  ( umask 077; : > "$ENVF" )
  echo "GEMINI_ROUTER_STATE=$SHARED/state.db" >> "$ENVF"
  if grep -qE '^GEMINI_API_KEYS=..*' "$EXTERNAL_ENV" 2>/dev/null; then
    grep -E '^GEMINI_API_KEYS=' "$EXTERNAL_ENV" >> "$ENVF"
    echo "imported GEMINI_API_KEYS from $EXTERNAL_ENV (shared pool, decision D2)"
  else
    echo "GEMINI_API_KEYS=" >> "$ENVF"
    echo "WARNING: no keys found in $EXTERNAL_ENV — edit $ENVF and fill GEMINI_API_KEYS" >&2
  fi
  echo "GEMINI_MCP_TOKEN=$(openssl rand -hex 32)" >> "$ENVF"
  chown "$APP_USER:$APP_USER" "$ENVF"
  echo "created $ENVF (chmod 600)"
fi
chmod 600 "$ENVF"

# ---- venv + deps ----
[[ -d $APP/.venv ]] || sudo -u "$APP_USER" uv venv "$APP/.venv"
as_app() { sudo -u "$APP_USER" env VIRTUAL_ENV="$APP/.venv" "$@"; }
as_app uv pip install --no-cache "$ROUTER_SRC"
as_app uv pip install --no-cache "mcp[cli]>=1.9" "uvicorn>=0.30" "starlette>=0.37" \
  "rich>=13.7" "pyyaml>=6.0"
# --no-deps: the app's pyproject pins gemini-router via a PRIVATE git URL; the router is
# already installed from local source above, so we skip dependency resolution entirely.
as_app uv pip install --no-cache --no-deps "$APP"

# ---- systemd unit (loopback only; no firewall changes needed) ----
cat > /etc/systemd/system/gemini-mcp.service <<UNIT
[Unit]
Description=gemini-mcp — Gemini MCP gateway (loopback HTTP daemon :8790)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP
EnvironmentFile=$ENVF
ExecStart=$APP/.venv/bin/gemini-mcp serve
Restart=on-failure
RestartSec=3
MemoryMax=350M
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=$APP $SHARED

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable gemini-mcp.service >/dev/null 2>&1 || true
systemctl restart gemini-mcp.service
sleep 2
systemctl --no-pager status gemini-mcp.service | sed -n '1,5p' || true

# ---- offline sanity (no quota spent; online checks live in smoke.sh) ----
sudo -u "$APP_USER" bash -c "set -a; . '$ENVF'; set +a; cd '$APP' && '$APP/.venv/bin/gemini-mcp' doctor --offline"

echo
echo "SETUP OK. Daemon: http://127.0.0.1:8790 (bearer in $ENVF → GEMINI_MCP_TOKEN)."
echo "Next: bash $APP/scripts/smoke.sh   # first real-key run, spends ≤1 RPD"
