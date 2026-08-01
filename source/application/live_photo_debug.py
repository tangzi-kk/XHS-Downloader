"""Safe, read-only diagnostics for XHS live-photo metadata."""

from __future__ import annotations

import hmac
import os
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import Body, Header, HTTPException

from source.expansion import Namespace

_RELEVANT_KEYWORDS = (
    "stream",
    "live",
    "video",
    "master",
    "backup",
    "h264",
    "h265",
    "media",
)
_VIDEO_MARKERS = (
    ".mp4",
    ".mov",
    ".m3u8",
    "sns-video",
    "video",
)


def _authorize(debug_token: str | None) -> None:
    token = (
        os.getenv("XHS_DEBUG_TOKEN", "").strip()
        or os.getenv("COZE_API_TOKEN", "").strip()
    )
    if not token:
        raise HTTPException(
            status_code=500,
            detail="Missing XHS_DEBUG_TOKEN or COZE_API_TOKEN",
        )
    supplied = (debug_token or "").strip()
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _sanitize_url(value: str) -> str:
    """Remove query strings and fragments so xsec_token cannot leak."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _is_relevant_path(path: str) -> bool:
    lowered = path.casefold()
    return any(keyword in lowered for keyword in _RELEVANT_KEYWORDS)


def _looks_like_video_url(value: str) -> bool:
    lowered = value.casefold()
    return value.startswith(("http://", "https://")) and any(
        marker in lowered for marker in _VIDEO_MARKERS
    )


def _collect_candidates(value: Any, path: str = "imageList") -> tuple[list[dict], list[dict]]:
    paths: list[dict] = []
    urls: list[dict] = []

    if isinstance(value, SimpleNamespace):
        value = vars(value)

    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            relevant = _is_relevant_path(child_path)
            if relevant:
                paths.append(
                    {
                        "path": child_path,
                        "value_type": type(child).__name__,
                    }
                )
            child_paths, child_urls = _collect_candidates(child, child_path)
            paths.extend(child_paths)
            urls.extend(child_urls)
        return paths, urls

    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            child_paths, child_urls = _collect_candidates(child, child_path)
            paths.extend(child_paths)
            urls.extend(child_urls)
        return paths, urls

    if isinstance(value, str):
        sanitized = _sanitize_url(value)
        if sanitized and (_is_relevant_path(path) or _looks_like_video_url(value)):
            urls.append(
                {
                    "path": path,
                    "url": sanitized,
                    "looks_like_video": _looks_like_video_url(value),
                }
            )

    return paths, urls


def _deduplicate(items: list[dict], keys: tuple[str, ...]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple] = set()
    for item in items:
        signature = tuple(item.get(key) for key in keys)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(item)
    return result


def install_live_photo_debug_route(xhs_cls) -> None:
    """Attach the diagnostic route while preserving all existing routes."""
    if getattr(xhs_cls, "_live_photo_debug_route_installed", False):
        return

    original_setup_routes = xhs_cls.setup_routes

    def setup_routes(self, server):
        original_setup_routes(self, server)

        @server.post("/xhs/debug/live-photo", tags=["API"])
        async def debug_live_photo(
            url: str = Body(..., embed=True),
            debug_token: str | None = Header(
                default=None,
                alias="X-Debug-Token",
            ),
        ):
            _authorize(debug_token)
            clean_url = str(url or "").strip()
            if not clean_url:
                raise HTTPException(status_code=400, detail="url is required")

            links = await self.extract_links(clean_url)
            if not links:
                raise HTTPException(
                    status_code=422,
                    detail="提取小红书作品链接失败",
                )

            cookie = os.getenv("XHS_COOKIE", "").strip() or None
            note_id, namespace = await self._get_html_data(
                links[0],
                True,
                cookie,
            )
            if not isinstance(namespace, Namespace):
                raise HTTPException(
                    status_code=502,
                    detail="获取小红书原始作品数据失败",
                )

            images = namespace.safe_extract("imageList", [])
            paths, candidate_urls = _collect_candidates(images)
            candidate_urls = _deduplicate(candidate_urls, ("path", "url"))
            paths = _deduplicate(paths, ("path", "value_type"))

            return {
                "note_id": note_id,
                "canonical_url": _sanitize_url(links[0]),
                "image_count": len(images) if isinstance(images, list) else 0,
                "candidate_paths": paths,
                "candidate_urls": candidate_urls,
                "video_candidate_count": sum(
                    1 for item in candidate_urls if item["looks_like_video"]
                ),
            }

    xhs_cls.setup_routes = setup_routes
    xhs_cls._live_photo_debug_route_installed = True
