"""The dependency floor is a promise, and 0.20.4 broke it: a string replacement missed
(`,<0.2` in the line), the wheel shipped declaring `gemini-router>=0.1.13`, and a fresh
install could pair this package with a router without the behaviour it relies on. The
build cannot notice that on its own — this test is the notice."""
from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.version import Version

# First router release with each capability this package depends on:
#   0.1.14 — keyauth (key transports, probe_key)
#   0.1.15 — file jobs stay on file-capable models
ROUTER_FLOOR = Version("0.1.15")


def _router_requirement() -> str:
    # Repo root via __file__, never cwd: the suite runs with cwd pinned to a tmp dir
    # so no test can pick up the checkout it happens to be invoked from.
    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    reqs = pyproject["project"]["dependencies"]
    return next(r for r in reqs if r.startswith("gemini-router"))


def test_router_floor_covers_the_capabilities_we_use():
    req = _router_requirement()
    floor = Version(req.split(">=")[1].split(",")[0].strip())
    assert floor >= ROUTER_FLOOR, (
        f"pyproject declares {req!r} — the code needs >= {ROUTER_FLOOR} "
        f"(key transports: 0.1.14; file-capable jobs: 0.1.15)")
