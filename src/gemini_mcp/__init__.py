"""gemini-mcp — MCP gateway for massive-payload Gemini analysis (free tier)."""
from .config import AppCfg, load_all
from .pipeline import AnalyzeParams, Pipeline, PlanBudgetError
from .progress import ProgressEmitter
from .tools import Deps

__version__ = "0.2.1"

__all__ = ["AppCfg", "load_all", "Pipeline", "AnalyzeParams", "PlanBudgetError",
           "ProgressEmitter", "Deps", "__version__"]
