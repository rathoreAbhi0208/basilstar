"""
news — Real-time AI-enriched Indian Financial Intelligence package.

Exports
~~~~~~~
    router               The main FastAPI APIRouter serving /news
    earnings_router      The earnings FastAPI APIRouter serving /news/earnings
    init_news_module     Startup initializer
    close_news_module    Shutdown handler
"""
from .api import router, init_news_module, close_news_module

__all__ = ["router", "init_news_module", "close_news_module"]
