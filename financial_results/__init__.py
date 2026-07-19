"""
financial_results — AI-powered Financial Results Analysis package.

Exports
~~~~~~~
    router                  The FastAPI APIRouter serving /financial-results
    init_results_module     Lifespan start-up hook
    close_results_module    Lifespan shut-down hook
"""
from .api import router, init_results_module, close_results_module

__all__ = ["router", "init_results_module", "close_results_module"]
