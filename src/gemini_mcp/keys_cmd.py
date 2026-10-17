"""`gemini keys` — one command to manage the API-key pool on whatever host runs
the gateway.

    gemini keys                 # list: fingerprints, masks, where the pool lives
    gemini keys add <key>…      # validate → probe (free) → write 0600 → restart
    gemini keys remove <ref>…   # by index, fingerprint prefix or the key itself

Why a command and not a doc: "edit the unit's EnvironmentFile and restart" is the kind of
procedure that is remembered until it is needed. The command detects the file itself (the
systemd unit's `EnvironmentFile=` when a unit exists, else the wizard's config home), so
the same incantation works on the VPS and on a laptop, and it prints what it did without
ever printing a key.

Rules kept: keys are validated before anything is written; a live probe (listModels — free,
no RPD) rejects a dead key unless `--force`; the write is atomic and 0600; duplicates are
never added twice; the last key is not removed without `--force`; the service restart is
skipped cleanly where there is no systemd.

Key shape is not a gate and not a guess: classic `AIza…` (Standard) and newer `AQ.…` (AI
Studio) keys both validate and both probe. The probe itself lives in the router's
`keyauth`, so it tries the same transports in the same order the daemon will and reports
which one answered — `ok (query)` here means the daemon will use the query transport too.
A key the provider has blocked as leaked is named as such, with the remedy, instead of a
bare 403.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from gemini_router import keyauth
from gemini_router.config import key_id as fingerprint

from .config import config_home

SERVICE = "gemini-mcp.service"
ENV_KEY = "GEMINI_API_KEYS"
DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"
HEALTHZ = "http://127.0.0.1:8790/healthz"

# Google keys are opaque tokens of [A-Za-z0-9_-.]: classic `AIzaSy…` and the newer
# `AQ.…` form both fit, and nothing with spaces/quotes ever does.
_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]{20,200}$")


class KeysError(RuntimeError):
    """Readable one-line failure; run() renders it, never a traceback."""


# ----------------------------------------------------------------------------- file side

def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True)


def service_env_file() -> Path | None:
    """The env file systemd feeds the service, when the unit exists and names one."""
    if not shutil.which("systemctl"):
        return None
    try:
        out = _systemctl("show", SERVICE, "-p", "EnvironmentFiles", "--value")
    except OSError:
        return None
    if out.returncode != 0:
        return None
    # value: "/opt/apps/gemini-mcp/.env (ignore_errors=no)" — possibly several entries
    for token in out.stdout.split():
        if token.startswith("/"):
            return Path(token)
    return None


def resolve_env_file(explicit: str | None) -> tuple[Path, str]:
    if explicit:
        return Path(explicit).expanduser(), "--env-file"
    unit = service_env_file()
    if unit:
        return unit, f"the systemd unit ({SERVICE})"
    return config_home() / ".env", "the wizard config home"


def read_keys(path: Path) -> list[str]:
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(ENV_KEY + "="):
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            return [k.strip() for k in raw.split(",") if k.strip()]
    return []


def write_keys(path: Path, keys: list[str]) -> None:
    """Atomic 0600 rewrite of just the one line; every other line survives verbatim."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    line = ENV_KEY + "=" + ",".join(keys)
    for i, existing in enumerate(lines):
        if existing.strip().startswith(ENV_KEY + "="):
            lines[i] = line
            break
    else:
        lines.append(line)
    body = "\n".join(lines).rstrip("\n") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        # mkstemp creates 0600 by construction, and os.replace keeps the source mode —
        # so a pool file hand-edited to 0644 is tightened to 0600 by the next write.
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def mask(key: str) -> str:
    return key[:6] + "…" + key[-4:] if len(key) > 12 else "…"


def validate(key: str) -> None:
    if not _KEY_RE.match(key):
        hint = (" (a key is 20+ chars of [A-Za-z0-9_-.] — check for a stray space, quote "
                "or newline)")
        raise KeysError(f"not a key-like string: {mask(key) if len(key) > 12 else key!r}{hint}")


# ---------------------------------------------------------------------------- live side

def probe(key: str, base: str | None = None) -> str:
    """Free liveness check: listModels, over whichever transport this key speaks.

    One implementation for CLI and daemon (the router's `keyauth`): the same transports in
    the same order, so what this prints is what generateContent will do. `ok` means the
    standard header; `ok (query)` / `ok (bearer)` mean the key needed that transport and it
    is now remembered for the process."""
    res = keyauth.probe_key(key, base or os.environ.get("GEMINI_BASE_URL") or DEFAULT_BASE)
    if not res["ok"]:
        return res["reason"]
    return "ok" if res["transport"] == keyauth.DEFAULT_TRANSPORT else f"ok ({res['transport']})"


def restart_service() -> str:
    if not shutil.which("systemctl"):
        return "skipped (no systemd)"
    out = _systemctl("restart", SERVICE)
    if out.returncode != 0:
        return f"failed ({out.stderr.strip()[:120] or 'systemctl returned ' + str(out.returncode)})"
    return "restarted"


def wait_healthz(timeout_s: float = 10.0) -> str:
    import time

    import httpx

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(HEALTHZ, timeout=2.0).status_code == 200:
                return "ok"
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return "no answer (the daemon may not be exposed on this host)"


# ----------------------------------------------------------------------------- command

def _pool_view(keys: list[str], check: bool) -> list[dict]:
    out = []
    for i, k in enumerate(keys, 1):
        row = {"index": i, "fingerprint": fingerprint(k), "masked": mask(k),
               "length": len(k)}
        if check:
            row["probe"] = probe(k)
        out.append(row)
    return out


def _print_rows(rows: list[dict]) -> None:
    for row in rows:
        line = f"  {row['index']}) {row['fingerprint']}  {row['masked']:<20} {row['length']} chars"
        if "probe" in row:
            line += f"  {row['probe']}"
        print(line)


def run(args) -> int:
    explicit = getattr(args, "env_file", None)
    as_json = getattr(args, "json", False)
    offline = getattr(args, "offline", False)
    no_restart = getattr(args, "no_restart", False)
    force = getattr(args, "force", False)
    action = args.action
    items = list(args.items or [])

    try:
        path, source = resolve_env_file(explicit)
        keys = read_keys(path)
        report: dict = {"ok": True, "env_file": str(path), "source": source,
                        "before": len(keys), "actions": []}

        if action == "list":
            rows = _pool_view(keys, getattr(args, "check", False))
            rejected = [r for r in rows if "probe" in r and not r["probe"].startswith("ok")]
            report["keys"] = rows
            report["rejected"] = [r["fingerprint"] for r in rejected]
            if as_json:
                print(json.dumps(report, indent=1))
            else:
                print(f"key pool: {path}  ({source})")
                _print_rows(rows) if rows else print("  (empty — add one: gemini "
                                                     "keys add <key>)")
                if rejected:
                    # The command to drop them exists; a pool that lists the dead next to
                    # the living should say so, or the operator goes looking for one
                    # (owner, 2026-10-11 — added the key, asked where removal was).
                    print(f"  {len(rejected)} rejected — remove: gemini keys remove "
                          + " ".join(r["fingerprint"] for r in rejected))
                elif rows and "probe" not in rows[0]:
                    print("  next: gemini keys list --check probes each key (free)"
                          " · gemini keys remove <n|fp> drops one")
            if rejected:
                return 1
            return 0

        if action == "add":
            if not items:
                raise KeysError("nothing to add; usage: gemini keys add <key>…")
            have = {fingerprint(k) for k in keys}
            added = 0
            for k in items:
                validate(k)
                if k in keys or fingerprint(k) in have:
                    report["actions"].append({"key": mask(k), "fingerprint": fingerprint(k),
                                              "result": "already present"})
                    continue
                if not offline:
                    verdict = probe(k)
                    if not verdict.startswith("ok") and not force:
                        raise KeysError(f"key {mask(k)} failed the live probe: {verdict} "
                                        "(use --offline to skip, --force to keep anyway)")
                    report["actions"].append({"key": mask(k), "fingerprint": fingerprint(k),
                                              "probe": verdict})
                keys.append(k)
                have.add(fingerprint(k))
                added += 1

        elif action == "remove":
            if not items:
                raise KeysError("nothing to remove; usage: gemini keys remove <n|fp|key>…")
            for ref in items:
                idx = None
                if ref.isdigit() and 1 <= int(ref) <= len(keys):
                    idx = int(ref) - 1
                else:
                    idx = next((i for i, k in enumerate(keys)
                                if k == ref or fingerprint(k).startswith(ref)
                                or fingerprint(ref) == fingerprint(k)), None)
                if idx is None:
                    raise KeysError(f"no key matches {ref!r} (use `gemini keys` "
                                    "to list the pool)")
                if len(keys) == 1 and not force:
                    raise KeysError("that is the only key; removing it empties the pool "
                                    "(use --force if that is really intended)")
                gone = keys.pop(idx)
                report["actions"].append({"key": mask(gone), "fingerprint": fingerprint(gone),
                                          "result": "removed"})
        else:  # pragma: no cover - argparse choices prevent this
            raise KeysError(f"unknown action {action!r}")

        if keys != read_keys(path):
            write_keys(path, keys)
        report["after"] = len(keys)

        if action != "list" and not no_restart:
            report["restart"] = restart_service()
            if report["restart"] == "restarted":
                report["healthz"] = wait_healthz()
        report["keys"] = _pool_view(keys, check=False)
    except KeysError as e:
        if as_json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=1))
        else:
            print(f"keys: {e}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(report, indent=1))
    else:
        if action != "list":
            for a in report["actions"]:
                extra = f"  probe: {a['probe']}" if "probe" in a else ""
                print(f"  {a.get('result', 'added')}: {a['fingerprint']} {a['key']}{extra}")
            print(f"pool: {report['before']} → {report['after']} key(s) in {path}")
            if "restart" in report:
                print(f"service: {report['restart']}"
                      + (f" · healthz: {report['healthz']}" if "healthz" in report else ""))
            print("next: `gemini keys` lists the pool · `gemini keys remove "
                  "<n|fp>` drops a dead one · `gemini doctor` checks keys")
    return 0
