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
import json

import httpx

from .config import settings
from .models import ImageResult
from .db import NewsDB

logger = logging.getLogger(__name__)

class ImageProvider(Protocol):
    @property
    def name(self) -> str: ...
    
    async def search(self, query: str) -> ImageResult | None: ...


class WikimediaCommonsProvider:
    @property
    def name(self) -> str:
        return "Wikimedia"

    async def search(self, query: str) -> ImageResult | None:
        url = "https://commons.wikimedia.org/w/api.php"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/xml,text/xml,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        params = {
            "action": "query",
            "format": "json",
            "prop": "imageinfo",
            "iiprop": "url|size|mime",
            "generator": "search",
            "gsrnamespace": 6,
            "gsrlimit": 10,
            "gsrsearch": query
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            
            pages = data.get("query", {}).get("pages", {})
            if not pages:
                return None
                
            results = []
            for page in pages.values():
                imageinfo = page.get("imageinfo")
                if not imageinfo:
                    continue
                info = imageinfo[0]
                
                width = info.get("width", 0)
                height = info.get("height", 0)
                mime = info.get("mime", "")
                img_url = info.get("url", "")
                
                if width < 300 or height < 300:
                    continue
                if not img_url:
                    continue
                if "image" not in mime:
                    continue
                    
                if "deleted" in img_url.lower():
                    continue

                score = 0
                if "svg" not in mime.lower():
                    score += 10
                if width > height:
                    score += 5
                    
                results.append((score, width * height, {
                    "url": img_url,
                    "width": width,
                    "height": height,
                    "descriptionurl": info.get("descriptionurl", "")
                }))
                
            if not results:
                return None
                
            results.sort(key=lambda x: (x[0], x[1]), reverse=True)
            best = results[0][2]
            
            return ImageResult(
                image_url=best["url"],
                thumbnail_url=best["url"],
                provider=self.name,
                photographer="Wikimedia Commons",
                photographer_url=best["descriptionurl"],
                width=best["width"],
                height=best["height"],
                license="Public Domain / CC"
            )


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

class ImageResolver:
    def __init__(self, db: NewsDB) -> None:
        self.db = db
        self.providers: list[ImageProvider] = []
        
        # Initialize providers based on config priority
        provider_map = {
            "wikimedia": WikimediaCommonsProvider,
            "pexels": PexelsProvider,
            "unsplash": UnsplashProvider,
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
            except httpx.TimeoutException as e:
                logger.warning("[%s] Timeout: %s", provider.name, e)
            except httpx.HTTPStatusError as e:
                logger.warning("[%s] HTTPStatusError: %s", provider.name, e)
            except json.JSONDecodeError as e:
                logger.warning("[%s] JSONDecodeError: %s", provider.name, e)
            except httpx.NetworkError as e:
                logger.warning("[%s] NetworkError: %s", provider.name, e)
            except ConnectionError as e:
                logger.warning("[%s] ConnectionError: %s", provider.name, e)
            except Exception as e:
                logger.warning("[%s] Unexpected error: %s", provider.name, e)
                
        return None
