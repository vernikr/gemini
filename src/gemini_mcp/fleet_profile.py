"""Local, non-secret connection profile for the user's configured VPS fleet."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import config_home


@dataclass(frozen=True, slots=True)
class FleetProfile:
    host: str
    user: str = "root"


def profile_path() -> Path:
    return config_home() / "fleet.json"


def load_profile() -> FleetProfile | None:
    path = profile_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        host = str(data.get("host", "")).strip()
        user = str(data.get("user", "root")).strip() or "root"
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError(f"fleet profile is unreadable: {path}") from exc
    if not host or any(c.isspace() for c in host) or any(c.isspace() for c in user):
        raise ValueError(f"fleet profile has an invalid host or user: {path}")
    return FleetProfile(host=host, user=user)


def save_profile(host: str, user: str = "root") -> Path:
    host, user = host.strip(), user.strip() or "root"
    if not host or any(c.isspace() for c in host) or any(c.isspace() for c in user):
        raise ValueError("VPS host and SSH user must be non-empty and contain no whitespace")
    path = profile_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps({"host": host, "user": user}, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".fleet-", dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return path
