"""
crawler/robots.py - robots.txt の取得・パース・判定
"""
from __future__ import annotations
import asyncio
import logging
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import aiohttp

log = logging.getLogger(__name__)

# ドメインごとのキャッシュ
_cache: dict[str, RobotFileParser | None] = {}
_lock = asyncio.Lock()


async def fetch_robots(domain: str, session: aiohttp.ClientSession,
                       user_agent: str, timeout: int = 10) -> RobotFileParser | None:
    """robots.txt を非同期で取得してパースする。キャッシュあり。"""
    async with _lock:
        if domain in _cache:
            return _cache[domain]

    robots_url = f"https://{domain}/robots.txt"
    try:
        async with session.get(robots_url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 200:
                text = await resp.text(errors="replace")
                rp = RobotFileParser()
                rp.set_url(robots_url)
                rp.parse(text.splitlines())
                log.debug(f"robots.txt 取得: {domain}")
            else:
                rp = None
    except Exception as e:
        log.debug(f"robots.txt 取得失敗 ({domain}): {e}")
        rp = None

    async with _lock:
        _cache[domain] = rp

    return rp


def is_allowed(rp: RobotFileParser | None, url: str, user_agent: str) -> bool:
    """クロール許可チェック。robots.txtがなければ許可とみなす。"""
    if rp is None:
        return True
    try:
        return rp.can_fetch(user_agent, url)
    except Exception:
        return True


def clear_cache() -> None:
    _cache.clear()
