"""Unsplash 图片服务(异步)。"""

import logging
from typing import Any, Dict, List, Optional

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)


class UnsplashService:
    """Unsplash 图片服务。"""

    def __init__(self) -> None:
        settings = get_settings()
        self.access_key = settings.unsplash_access_key
        self.base_url = "https://api.unsplash.com"

    async def search_photos(self, query: str, per_page: int = 5) -> List[Dict[str, Any]]:
        """按关键字搜索图片。"""
        if not self.access_key:
            logger.warning("未配置 UNSPLASH_ACCESS_KEY, 跳过图片搜索")
            return []

        params = {"query": query, "per_page": per_page, "client_id": self.access_key}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/search/photos", params=params)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # noqa: BLE001 - 图片属于锦上添花, 失败不影响主流程
            logger.warning("Unsplash 搜索失败: %s", exc)
            return []

        photos: List[Dict[str, Any]] = []
        for photo in data.get("results", []):
            photos.append(
                {
                    "id": photo.get("id"),
                    "url": photo.get("urls", {}).get("regular"),
                    "thumb": photo.get("urls", {}).get("thumb"),
                    "description": photo.get("description") or photo.get("alt_description"),
                    "photographer": photo.get("user", {}).get("name"),
                }
            )
        return photos

    async def get_photo_url(self, query: str) -> Optional[str]:
        """获取单张图片 URL。"""
        photos = await self.search_photos(query, per_page=1)
        return photos[0].get("url") if photos else None


_unsplash_service: Optional[UnsplashService] = None


def get_unsplash_service() -> UnsplashService:
    """获取 Unsplash 服务(单例)。"""
    global _unsplash_service
    if _unsplash_service is None:
        _unsplash_service = UnsplashService()
    return _unsplash_service
