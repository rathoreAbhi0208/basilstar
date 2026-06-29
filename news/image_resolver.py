"""
news/image_resolver.py
----------------------
Handles fetching high-quality stock images from multiple providers
with automatic fallback and caching.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Protocol, Optional

import httpx

from .config import settings
from .models import ImageResult, NewsArticle
from .db import NewsDB

logger = logging.getLogger(__name__)

class ImageProvider(Protocol):
    @property
    def name(self) -> str: ...
    
    async def search(self, query: str) -> ImageResult | None: ...


class PexelsProvider:
    def __init__(self) -> None:
        self.api_key = settings.pexels_api_key
        self.base_url = settings.pexels_base_url
        
    @property
    def name(self) -> str:
        return "Pexels"

    async def search(self, query: str) -> ImageResult | None:
        if not self.api_key:
            return None
            
        url = f"{self.base_url}/search"
        params = {"query": query, "per_page": 1, "orientation": "landscape"}
        headers = {"Authorization": self.api_key}
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            
            if not data.get("photos"):
                return None
                
            photo = data["photos"][0]
            return ImageResult(
                image_url=photo["src"]["large"],
                thumbnail_url=photo["src"]["medium"],
                provider=self.name,
                photographer=photo.get("photographer", "Unknown"),
                photographer_url=photo.get("photographer_url", ""),
                width=photo.get("width", 0),
                height=photo.get("height", 0),
                license="Pexels License"
            )


class UnsplashProvider:
    def __init__(self) -> None:
        self.api_key = settings.unsplash_access_key
        self.base_url = settings.unsplash_base_url
        
    @property
    def name(self) -> str:
        return "Unsplash"

    async def search(self, query: str) -> ImageResult | None:
        if not self.api_key:
            return None
            
        url = f"{self.base_url}/search/photos"
        params = {"query": query, "per_page": 1, "orientation": "landscape"}
        headers = {"Authorization": f"Client-ID {self.api_key}", "Accept-Version": "v1"}
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            
            if not data.get("results"):
                return None
                
            photo = data["results"][0]
            return ImageResult(
                image_url=photo["urls"]["regular"],
                thumbnail_url=photo["urls"]["small"],
                provider=self.name,
                photographer=photo["user"].get("name", "Unknown"),
                photographer_url=photo["user"]["links"].get("html", ""),
                width=photo.get("width", 0),
                height=photo.get("height", 0),
                license="Unsplash License"
            )


class PixabayProvider:
    def __init__(self) -> None:
        self.api_key = settings.pixabay_api_key
        self.base_url = settings.pixabay_base_url
        
    @property
    def name(self) -> str:
        return "Pixabay"

    async def search(self, query: str) -> ImageResult | None:
        if not self.api_key:
            return None
            
        url = self.base_url
        params = {
            "key": self.api_key,
            "q": urllib.parse.quote(query),
            "image_type": "photo",
            "orientation": "horizontal",
            "per_page": 3
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            
            if not data.get("hits"):
                return None
                
            photo = data["hits"][0]
            return ImageResult(
                image_url=photo.get("largeImageURL", photo.get("webformatURL", "")),
                thumbnail_url=photo.get("previewURL", ""),
                provider=self.name,
                photographer=photo.get("user", "Unknown"),
                photographer_url=f"https://pixabay.com/users/{photo.get('user_id', '')}",
                width=photo.get("imageWidth", 0),
                height=photo.get("imageHeight", 0),
                license="Pixabay License"
            )


class ImageResolver:
    def __init__(self, db: NewsDB) -> None:
        self.db = db
        self.providers: list[ImageProvider] = []
        
        # Initialize providers based on config priority
        provider_map = {
            "pexels": PexelsProvider,
            "unsplash": UnsplashProvider,
            "pixabay": PixabayProvider
        }
        
        for p_name in settings.image_provider_priority:
            if p_name in provider_map:
                self.providers.append(provider_map[p_name]())
                
    async def resolve(self, query: str | None) -> ImageResult | None:
        if not query:
            return None
            
        # 1. Check Cache
        try:
            cached = await self.db.get_cached_image(query)
            if cached:
                # Return partial ImageResult (only url and provider are really needed by the rest of the app)
                return ImageResult(
                    image_url=cached["image_url"],
                    thumbnail_url="",
                    provider=cached["provider"],
                    photographer="",
                    photographer_url="",
                    width=0,
                    height=0,
                    license=""
                )
        except Exception as e:
            logger.warning("[ImageResolver] Cache lookup failed: %s", e)

        # 2. Fallback Chain
        for provider in self.providers:
            try:
                logger.info("[%s] Searching \"%s\"", provider.name, query)
                result = await provider.search(query)
                if result:
                    logger.info("[%s] Image found", provider.name)
                    logger.info("Selected provider: %s", provider.name)
                    
                    # Store in cache
                    try:
                        await self.db.set_cached_image(query, result.image_url, result.provider)
                    except Exception as e:
                        logger.warning("[ImageResolver] Cache set failed: %s", e)
                        
                    return result
                else:
                    logger.info("[%s] No results", provider.name)
            except Exception as e:
                logger.warning("[%s] Error: %s", provider.name, e)
                
        return None
