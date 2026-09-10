"""
crawler/crawler.py - requests 軽量クローラー
============================================================
固定パス巡回に加え、取得ページ内の内部リンクから製品・技術・開発・
採用・ニュース等の関連ページを優先探索する。
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import time
from collections import defaultdict
from itertools import count
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from company_analyzer.config import CRAWL_CFG, TARGET_PATHS
from company_analyzer.models import PageResult

log = logging.getLogger(__name__)

_domain_last_access: dict[str, float] = defaultdict(float)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

TIMEOUT = 12

# T2 R2を含む企業スクリーニングでEvidenceが出やすいページ。
# URLとアンカーテキストの双方を使うため、日本語サイトにも対応する。
_DISCOVERY_SIGNALS = {
    "product": 16,
    "products": 16,
    "製品": 16,
    "商品": 14,
    "technology": 16,
    "technologies": 16,
    "技術": 16,
    "development": 18,
    "develop": 14,
    "開発": 18,
    "research": 18,
    "r&d": 18,
    "rnd": 18,
    "研究": 18,
    "fpga": 24,
    "soc": 20,
    "linux": 20,
    "組込み": 20,
    "組み込み": 20,
    "回路": 20,
    "基板": 18,
    "画像処理": 20,
    "画像認識": 20,
    "ai": 10,
    "company": 10,
    "corporate": 10,
    "about": 10,
    "profile": 12,
    "outline": 12,
    "organization": 14,
    "会社": 10,
    "企業": 10,
    "組織": 14,
    "recruit": 18,
    "career": 18,
    "careers": 18,
    "jobs": 18,
    "採用": 18,
    "news": 14,
    "press": 14,
    "release": 16,
    "topics": 12,
    "ニュース": 14,
    "新製品": 20,
    "oem": 14,
    "odm": 14,
    "solution": 10,
    "solutions": 10,
}

_SKIP_SUFFIXES = (
    ".pdf", ".zip", ".jpg", ".jpeg", ".png", ".gif", ".svg",
    ".webp", ".mp4", ".mp3", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx",
)


def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=1, backoff_factor=0.5, status_forcelist=[500, 502, 503])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(HEADERS)
    return s


def _extract_domain(url: str) -> str:
    return urlparse(url).netloc or url


def _host_key(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def _normalize_url(base: str, path: str) -> str:
    if path.startswith("http"):
        return path
    return urljoin(base, path)


def _canonical_url(url: str) -> str:
    p = urlparse(url)
    path = p.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query, ""))


def _fetch_page_sync(session: requests.Session, url: str) -> PageResult:
    """1ページを同期的に取得"""
    start = time.monotonic()
    try:
        resp = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code in (404, 410, 403):
            return PageResult(url=url, status_code=resp.status_code)

        if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"

        html = resp.text
        return PageResult(url=url, status_code=resp.status_code, html=html, elapsed_ms=elapsed_ms)

    except requests.exceptions.Timeout:
        return PageResult(url=url, error="timeout")
    except requests.exceptions.SSLError:
        try:
            resp = session.get(url, timeout=TIMEOUT, allow_redirects=True, verify=False)
            if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding or "utf-8"
            return PageResult(
                url=url,
                status_code=resp.status_code,
                html=resp.text,
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as e2:
            return PageResult(url=url, error=f"ssl_error: {str(e2)[:60]}")
    except Exception as e:
        return PageResult(url=url, error=f"{type(e).__name__}: {str(e)[:60]}")


def _link_score(url: str, anchor_text: str) -> int:
    """企業評価Evidenceとして有用そうな内部リンクへ優先度を付ける。"""
    haystack = f"{url.lower()} {anchor_text.lower()}"
    score = 0
    for signal, weight in _DISCOVERY_SIGNALS.items():
        if signal.lower() in haystack:
            score += weight
    return score


def _discover_candidate_links(html: str, current_url: str, site_url: str) -> list[tuple[int, str]]:
    """HTMLから同一サイト内の評価関連リンクを抽出して優先度順に返す。"""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    site_host = _host_key(site_url)
    found: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(current_url, href)
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        if _host_key(full) != site_host:
            continue
        if parsed.path.lower().endswith(_SKIP_SUFFIXES):
            continue

        canonical = _canonical_url(full)
        anchor = a.get_text(" ", strip=True)[:120]
        score = _link_score(canonical, anchor)
        if score <= 0:
            continue
        if score > found.get(canonical, 0):
            found[canonical] = score

    return sorted(((score, url) for url, score in found.items()), reverse=True)


def crawl_site_sync(base_url: str, session: requests.Session, paths: list[str] | None = None) -> list[PageResult]:
    """1社分を固定パス + 関連内部リンク探索でクロールする。"""
    cfg = CRAWL_CFG
    results: list[PageResult] = []
    _paths = paths if paths else TARGET_PATHS

    # 優先度付きキュー。トップページを必ず最初に取得する。
    seq = count()
    heap: list[tuple[int, int, str]] = []
    queued: set[str] = set()
    attempted: set[str] = set()

    def enqueue(url: str, priority: int) -> None:
        canonical = _canonical_url(url)
        if canonical in queued or canonical in attempted:
            return
        queued.add(canonical)
        heapq.heappush(heap, (-priority, next(seq), canonical))

    enqueue(_normalize_url(base_url, "/"), 1000)
    for path in _paths:
        if path == "/":
            continue
        enqueue(_normalize_url(base_url, path), 5)

    # 404候補が多数あるサイトでも無限に試さない。
    max_attempts = max(cfg.max_pages_per_site * 5, 40)
    attempts = 0

    while heap and len(results) < cfg.max_pages_per_site and attempts < max_attempts:
        _, _, url = heapq.heappop(heap)
        queued.discard(url)
        if url in attempted:
            continue
        attempted.add(url)
        attempts += 1

        result = _fetch_page_sync(session, url)
        if result.error:
            log.debug(f"  スキップ ({result.error}): {url}")
            if len(attempted) == 1:
                break
            continue
        if result.status_code in (403, 404, 410):
            continue
        if result.status_code != 200 or not result.html:
            continue

        results.append(result)

        # 成功ページからさらに関連する深いページを発見する。
        # 例: /about -> /about/outline/executive, /recruit -> /recruit/jobs
        discovered = _discover_candidate_links(result.html, url, base_url)
        for score, discovered_url in discovered[:20]:
            enqueue(discovered_url, 100 + score)

    domain = _extract_domain(base_url)
    elapsed = time.monotonic() - _domain_last_access[domain]
    wait = getattr(cfg, "delay_per_domain", 1.0) - elapsed
    if wait > 0:
        time.sleep(min(wait, 1.0))
    _domain_last_access[domain] = time.monotonic()

    return results


async def crawl_all(
    companies: list[dict],
    concurrency: int | None = None,
    paths: list[str] | None = None,
) -> dict[str, list[PageResult]]:
    """
    全企業を並列クロール（requests版・スレッドプール使用）。
    pathsは固定シードURLとして扱い、取得ページから関連内部リンクも探索する。
    """
    cfg = CRAWL_CFG
    _paths = paths if paths else TARGET_PATHS
    max_workers = min(concurrency or cfg.concurrency, 8)
    results: dict[str, list[PageResult]] = {}
    url_to_name = {c["url"]: c.get("name", c["url"]) for c in companies if c.get("url")}
    total = len(url_to_name)

    log.info(f"  対象: {total}社 / 同時接続: {max_workers}（requests軽量版）")

    done_count = 0
    error_count = 0
    sem = asyncio.Semaphore(max_workers)

    async def _crawl_one(url: str, name: str) -> None:
        nonlocal done_count, error_count
        async with sem:
            loop = asyncio.get_event_loop()
            session = _make_session()
            try:
                pages = await loop.run_in_executor(
                    None, crawl_site_sync, url, session, _paths
                )
                results[url] = pages
                page_count = len(pages)
                status = f"{page_count}p取得" if page_count > 0 else "取得0"
                pct = (done_count + 1) / total * 100
                log.info(
                    f"  [{done_count+1:4d}/{total}] ({pct:5.1f}%) "
                    f"{'✓' if page_count else '－'} {name[:30]:<30}  {status}"
                )
            except Exception as e:
                log.warning(f"  [{done_count+1:4d}/{total}] ✗ {name[:30]} → {e}")
                results[url] = []
                error_count += 1
            finally:
                session.close()
                done_count += 1
                if done_count % 100 == 0:
                    success = done_count - error_count
                    log.info(f"\n  ── {done_count}/{total}件完了 (成功:{success} エラー:{error_count}) ──\n")

    tasks = [_crawl_one(url, name) for url, name in url_to_name.items()]
    await asyncio.gather(*tasks)

    log.info(f"  クロール完了: {total}社 / エラー: {error_count}社")
    return results
