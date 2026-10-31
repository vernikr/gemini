#!/usr/bin/env bash
# setup_server.sh — idempotent provisioning of gemini on an owner-managed Linux host.
# Run with the privileges required to install the service and write its configuration.
# The daemon binds only to loopback; Python tooling is installed with uv (no global pip).
# Re-running is safe: dirs/venv/.env persist, deps are reinstalled, unit is restarted.
set -euo pipefail

APP=/opt/apps/gemini-mcp
ROUTER_SRC=/opt/apps/gemini-router          # rsync'd router source
SHARED=/opt/apps/shared/gemini-router       # shared quota ledger
APP_USER="${APP_USER:-root}"
ENVF="$APP/.env"

command -v uv >/dev/null 2>&1 || {
  echo "uv not found. Install as root: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1; }
id "$APP_USER" >/dev/null 2>&1 || { echo "user '$APP_USER' missing (override: APP_USER=...)" >&2; exit 1; }

# pandoc — server-side RTF/DOCX → Markdown extraction (M4.6). One-shot subprocess
# per file (no daemon, no RAM residency); never touches ports/services (host safety requirements).
if ! command -v pandoc >/dev/null 2>&1; then
  echo "→ installing pandoc (RTF/DOCX → Markdown)"
  apt-get update -qq >/dev/null 2>&1 || true
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq pandoc >/dev/null
fi

# poppler-utils — pdfinfo only: the page count a PDF pre-flight refuses on. The content
# of a PDF is never extracted locally (the provider reads it), so this stays a one-shot
# subprocess, like pandoc. Licence-clean: no Python dependency, no AGPL.
if ! command -v pdfinfo >/dev/null 2>&1; then
  echo "→ installing poppler-utils (pdfinfo: PDF page-count pre-flight)"
  apt-get update -qq >/dev/null 2>&1 || true
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq poppler-utils >/dev/null
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

# Upstream keys are deliberately application-specific: never import credentials from
# another project or account. Keep the variable present for the owner to populate.
if ! grep -q '^GEMINI_API_KEYS=' "$ENVF"; then
  echo "GEMINI_API_KEYS=" >> "$ENVF"
fi
if ! grep -qE '^GEMINI_API_KEYS=..*' "$ENVF"; then
  echo "WARNING: GEMINI_API_KEYS is empty in $ENVF" >&2
  echo "         → add the key pool for this Gemini deployment before starting the service" >&2
fi

# ---- venv + deps ----
[[ -d $APP/.venv ]] || sudo -u "$APP_USER" uv venv "$APP/.venv"
as_app() { sudo -u "$APP_USER" env VIRTUAL_ENV="$APP/.venv" "$@"; }
as_app uv pip install --no-cache "$ROUTER_SRC"
as_app uv pip install --no-cache "mcp[cli]>=1.9" "uvicorn>=0.30" "starlette>=0.37" \
  "rich>=13.7" "pyyaml>=6.0" "python-dotenv>=1.0"
# --no-deps: the router is already installed from the synchronized source checkout above;
# install the application package without replacing it from the package index.
as_app uv pip install --no-cache --no-deps "$APP"

# ---- released package via `uv tool` — the binary the systemd unit runs, the one
# `gemini smoke` exercises, and the one push-CD re-installs on every release
# (/usr/local/bin/deploy-tools does `uv tool install --force`).
# The .venv above is the SOURCE fallback: it installs gemini-router from local checkout,
# so it is the only path that works while a router version is unpublished on PyPI.
# Do not remove it while router releases stay manual.
UHOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
TOOL_BIN="$UHOME/.local/bin/gemini"
VER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$APP/pyproject.toml" | head -1)"
sudo -u "$APP_USER" env HOME="$UHOME" uv tool install --force "vernikr-gemini==$VER" \
  >/dev/null 2>&1 \
  || sudo -u "$APP_USER" env HOME="$UHOME" uv tool install --force vernikr-gemini \
  >/dev/null 2>&1
[[ -x $TOOL_BIN ]] || { echo "uv tool install vernikr-gemini failed (no $TOOL_BIN)" >&2; exit 1; }

# ---- config-home anchor for BARE ssh launches (Freebuff stdio → no EnvironmentFile):
# ~/.config/gemini → $APP makes .env/config.yaml/logs identical on every transport.
# Explicit -L check: `ln -sfn` into an existing dir would nest the link inside it.
mkdir -p "$UHOME/.config"
if [[ ! -L "$UHOME/.config/gemini" ]]; then
  rm -rf "$UHOME/.config/gemini"
  ln -s "$APP" "$UHOME/.config/gemini"
fi

# ---- push-CD whitelist entry (only when the dispatcher exists on this host) ----
if [[ -x /usr/local/bin/deploy-tools && ! -f /etc/deploy-tools.d/gemini.conf ]]; then
  cat > /etc/deploy-tools.d/gemini.conf <<CONF
INSTALLER=uv
PKG=vernikr-gemini
BIN=gemini
UNIT=gemini-mcp.service
STATUS_URL=http://127.0.0.1:8790/healthz
HEALTH_FIELD=version
CONF
  echo "created /etc/deploy-tools.d/gemini.conf (push-CD)"
fi

# ---- systemd unit (loopback only; no firewall changes needed) ----
cat > /etc/systemd/system/gemini-mcp.service <<UNIT
[Unit]
Description=gemini — Gemini MCP gateway (loopback HTTP daemon :8790)
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

# ---- offline sanity (no quota spent; online checks live in `gemini smoke`) ----
if sudo -u "$APP_USER" env HOME="$UHOME" bash -c "set -a; . '$ENVF'; set +a; cd '$APP' && '$TOOL_BIN' doctor --offline"; then
  echo
  echo "SETUP OK. Daemon: http://127.0.0.1:8790 (bearer in $ENVF → GEMINI_MCP_TOKEN)."
  echo "Next: gemini smoke   # first real-key run, spends ≤1 RPD"
else
  echo
  # 3 = no API keys, which is the only failure this script can cause; anything else
  # (4 = state file unusable, 5 = no chain model reachable) needs the output above.
  echo "SETUP INCOMPLETE — doctor failed (output above). Most likely: GEMINI_API_KEYS empty."
  echo "Fix (on the server):"
  echo "  1) echo 'GEMINI_API_KEYS=<key1,key2>' >> $ENVF   # keys from AI Studio"
  echo "  2) systemctl restart gemini && systemctl is-active gemini"
  echo "  3) gemini smoke"
  echo "Note: without keys the daemon restart-loops by design (fail-fast); it recovers"
  echo "      automatically within seconds once keys are in $ENVF."
  exit 1
fi
