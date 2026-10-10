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

# pandoc — server-side RTF/DOCX → Markdown extraction (M4.6). One-shot subprocess
# per file (no daemon, no RAM residency); never touches ports/services (deployment notes).
if ! command -v pandoc >/dev/null 2>&1; then
  echo "→ installing pandoc (RTF/DOCX → Markdown)"
  apt-get update -qq >/dev/null 2>&1 || true
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq pandoc >/dev/null
fi

mkdir -p "$APP/logs" "$SHARED" "$ROUTER_SRC"
chown -R "$APP_USER:$APP_USER" "$APP" "$SHARED" "$ROUTER_SRC"
chmod 770 "$SHARED"

# ---- .env: created once, never overwritten (secrets stay on the server) ----
if [[ ! -f $ENVF ]]; then
  ( umask 077; : > "$ENVF" )
  echo "GEMINI_ROUTER_STATE=$SHARED/state.db" >> "$ENVF"
  echo "GEMINI_MCP_TOKEN=$(openssl rand -hex 32)" >> "$ENVF"
  chown "$APP_USER:$APP_USER" "$ENVF"
  echo "created $ENVF (chmod 600)"
fi
chmod 600 "$ENVF"

# Key import (decision D2: shared pool with external-consumer). Retried on EVERY run while
# GEMINI_API_KEYS is still empty. Source vars tried in order:
#   GEMINI_API_KEYS → GEMINI_API_KEY → LLM_API_KEY (only when LLM_PROVIDER contains
#   "gemini" — external-consumer uses generic provider names; canonical name here is
#   GEMINI_API_KEYS; M6 migration will unify external-consumer onto it).
if ! grep -qE '^GEMINI_API_KEYS=..*' "$ENVF"; then
  src=""
  for pat in '^GEMINI_API_KEYS=..*' '^GEMINI_API_KEY=..*'; do
    src="$(grep -hE "$pat" "$EXTERNAL_ENV" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    [[ -n $src ]] && break
  done
  if [[ -z $src ]] && grep -qiE '^LLM_PROVIDER=.*gemini' "$EXTERNAL_ENV" 2>/dev/null; then
    src="$(grep -hE '^LLM_API_KEY=.+' "$EXTERNAL_ENV" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    [[ -n $src ]] && echo "note: key taken from external-consumer LLM_API_KEY (LLM_PROVIDER=gemini)"
  fi
  sed -i '/^GEMINI_API_KEYS=/d' "$ENVF"
  if [[ -n $src ]]; then
    echo "GEMINI_API_KEYS=$src" >> "$ENVF"
    echo "imported Gemini key(s) from $EXTERNAL_ENV (shared pool)"
  else
    echo "GEMINI_API_KEYS=" >> "$ENVF"
    echo "WARNING: no Gemini keys found in $EXTERNAL_ENV" >&2
    echo "         (tried GEMINI_API_KEYS / GEMINI_API_KEY / LLM_API_KEY+LLM_PROVIDER=gemini)" >&2
    echo "         → put them into $ENVF as GEMINI_API_KEYS=key1,key2" >&2
  fi
fi

# ---- venv + deps ----
[[ -d $APP/.venv ]] || sudo -u "$APP_USER" uv venv "$APP/.venv"
as_app() { sudo -u "$APP_USER" env VIRTUAL_ENV="$APP/.venv" "$@"; }
as_app uv pip install --no-cache "$ROUTER_SRC"
as_app uv pip install --no-cache "mcp[cli]>=1.9" "uvicorn>=0.30" "starlette>=0.37" \
  "rich>=13.7" "pyyaml>=6.0" "python-dotenv>=1.0"
# --no-deps: the app's pyproject pins gemini-router via a PRIVATE git URL; the router is
# already installed from local source above, so we skip dependency resolution entirely.
as_app uv pip install --no-cache --no-deps "$APP"

# ---- released package via `uv tool` — the binary the systemd unit runs, the one
# `gemini-mcp smoke` exercises, and the one push-CD re-installs on every release
# (/usr/local/bin/deploy-tools does `uv tool install --force`).
# The .venv above is the SOURCE fallback: it installs gemini-router from local checkout,
# so it is the only path that works while a router version is unpublished on PyPI.
# Do not remove it while router releases stay manual.
UHOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
TOOL_BIN="$UHOME/.local/bin/gemini-mcp"
VER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$APP/pyproject.toml" | head -1)"
sudo -u "$APP_USER" env HOME="$UHOME" uv tool install --force "gemini-mcp-gateway==$VER" \
  >/dev/null 2>&1 \
  || sudo -u "$APP_USER" env HOME="$UHOME" uv tool install --force gemini-mcp-gateway \
  >/dev/null 2>&1
[[ -x $TOOL_BIN ]] || { echo "uv tool install gemini-mcp-gateway failed (no $TOOL_BIN)" >&2; exit 1; }

# ---- config-home anchor for BARE ssh launches (Freebuff stdio → no EnvironmentFile):
# ~/.config/gemini-mcp → $APP makes .env/config.yaml/logs identical on every transport.
# Explicit -L check: `ln -sfn` into an existing dir would nest the link inside it.
mkdir -p "$UHOME/.config"
if [[ ! -L "$UHOME/.config/gemini-mcp" ]]; then
  rm -rf "$UHOME/.config/gemini-mcp"
  ln -s "$APP" "$UHOME/.config/gemini-mcp"
fi

# ---- push-CD whitelist entry (only when the dispatcher exists on this host) ----
if [[ -x /usr/local/bin/deploy-tools && ! -f /etc/deploy-tools.d/gemini-mcp.conf ]]; then
  cat > /etc/deploy-tools.d/gemini-mcp.conf <<CONF
INSTALLER=uv
PKG=gemini-mcp-gateway
BIN=gemini-mcp
UNIT=gemini-mcp.service
STATUS_URL=http://127.0.0.1:8790/healthz
HEALTH_FIELD=version
CONF
  echo "created /etc/deploy-tools.d/gemini-mcp.conf (push-CD)"
fi

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
ExecStart=$TOOL_BIN serve
Restart=on-failure
RestartSec=3
MemoryMax=350M
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$APP $SHARED
# NOTE: no ProtectHome — it would hide $UHOME/.local (the uv-tool binary) from the unit.

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable gemini-mcp.service >/dev/null 2>&1 || true
systemctl restart gemini-mcp.service
sleep 2
systemctl --no-pager status gemini-mcp.service | sed -n '1,5p' || true

# ---- offline sanity (no quota spent; online checks live in `gemini-mcp smoke`) ----
if sudo -u "$APP_USER" env HOME="$UHOME" bash -c "set -a; . '$ENVF'; set +a; cd '$APP' && '$TOOL_BIN' doctor --offline"; then
  echo
  echo "SETUP OK. Daemon: http://127.0.0.1:8790 (bearer in $ENVF → GEMINI_MCP_TOKEN)."
  echo "Next: gemini-mcp smoke   # first real-key run, spends ≤1 RPD"
else
  echo
  # 3 = no API keys, which is the only failure this script can cause; anything else
  # (4 = state file unusable, 5 = no chain model reachable) needs the output above.
  echo "SETUP INCOMPLETE — doctor failed (output above). Most likely: GEMINI_API_KEYS empty."
  echo "Fix (on the server):"
  echo "  1) echo 'GEMINI_API_KEYS=<key1,key2>' >> $ENVF   # keys from AI Studio"
  echo "  2) systemctl restart gemini-mcp && systemctl is-active gemini-mcp"
  echo "  3) gemini-mcp smoke"
  echo "Note: without keys the daemon restart-loops by design (fail-fast); it recovers"
  echo "      automatically within seconds once keys are in $ENVF."
  exit 1
fi
