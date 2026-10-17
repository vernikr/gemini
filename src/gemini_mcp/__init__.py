"""gemini — MCP gateway for massive-payload Gemini analysis (free tier)."""
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .config import AppCfg, load_all
from .pipeline import AnalyzeParams, Pipeline, PlanBudgetError
from .progress import ProgressEmitter
from .tools import Deps

try:                                    # single source of truth = pyproject version
    __version__ = _pkg_version("vernikr-gemini")
except PackageNotFoundError:            # legacy dist name / unpackaged run
    try:
        __version__ = _pkg_version("gemini")
    except PackageNotFoundError:
        __version__ = "0.0.0.dev0"

__all__ = ["AppCfg", "load_all", "Pipeline", "AnalyzeParams", "PlanBudgetError",
           "ProgressEmitter", "Deps", "__version__"]
