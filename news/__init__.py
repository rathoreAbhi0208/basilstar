"""
news — Real-time AI-enriched Indian Financial Intelligence package.

Exports
~~~~~~~
    router   The single FastAPI APIRouter serving:
                   /news              — News pipeline
"""
from .api import router, init_news_module, close_news_module

__all__ = ["router", "init_news_module", "close_news_module"]
