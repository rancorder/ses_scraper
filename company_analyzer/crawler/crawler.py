"""
crawler/crawler.py - requests 軽量クローラー
============================================================
固定パス巡回に加え、取得ページ内の内部リンクから製品・技術・開発・
採用・ニュース・展示会・問い合わせ等の関連ページを優先探索する。
"""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import logging
import re
import time
from collections import defaultdict
from datetime import date
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
    "profile": 16,
    "outline": 14,
    "organization": 14,
    "location": 10,
    "会社概要": 18,
    "会社": 10,
    "企業": 10,
    "組織": 14,
    "拠点": 14,
    "研究所": 18,
    "技術センター": 18,
    "テクニカルセンター": 18,
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
    "event": 22,
    "events": 22,
    "exhibition": 26,
    "exhibitions": 26,
    "expo": 22,
    "展示会": 26,
    "出展": 26,
    "見本市": 22,
    "ブース": 20,
    "フェア": 16,
    "イベント": 20,
    "contact": 18,
    "inquiry": 18,
    "お問い合わせ": 18,
    "問い合わせ": 16,
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
_ARCHIVE_HINTS = (
    "news", "topics", "press", "release", "event", "exhibition",
    "newinformation",
)

# 技術Evidence 12ページ枠を維持しつつ、展示会/ニュース探索だけ追加で最大4ページ許容する。
# これにより製品・採用ページが多い企業でも展示会一覧→年別/2ページ目→詳細へ到達しやすくする。
_ARCHIVE_EXTRA_PAGES = 4
_ARCHIVE_SEED_PATHS = (
    "/news",
    "/topics",
    "/press",
    "/release",
    "/event",
    "/events",
    "/exhibition",
    "/news_exhibition",
    "/technology/event",
    "/newinformation",
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


def _html_fingerprint(html: str) -> str:
    normalized = re.sub(r"\s+", " ", html).strip()
    return hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()


def _fetch_page_sync(session: requests.Session, url: str) -> PageResult:
    start = time.monotonic()
    try:
        resp = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code in (404, 410, 403):
            return PageResult(url=resp.url or url, status_code=resp.status_code)

        if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"

        html = resp.text
        return PageResult(
            url=resp.url or url,
            status_code=resp.status_code,
            html=html,
            elapsed_ms=elapsed_ms,
        )

    except requests.exceptions.Timeout:
        return PageResult(url=url, error="timeout")
    except requests.exceptions.SSLError:
        try:
            resp = session.get(url, timeout=TIMEOUT, allow_redirects=True, verify=False)
            if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding or "utf-8"
            return PageResult(
                url=resp.url or url,
                status_code=resp.status_code,
                html=resp.text,
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as e2:
            return PageResult(url=url, error=f"ssl_error: {str(e2)[:60]}")
    except Exception as e:
        return PageResult(url=url, error=f"{type(e).__name__}: {str(e)[:60]}")


def _link_score(url: str, anchor_text: str) -> int:
    haystack = f"{url.lower()} {anchor_text.lower()}"
    score = 0
    for signal, weight in _DISCOVERY_SIGNALS.items():
        if signal.lower() in haystack:
            score += weight
    return score


def _page_number(url: str) -> int | None:
    path = urlparse(url).path.lower().rstrip("/")
    m = re.search(r"/page/(\d+)$", path)
    return int(m.group(1)) if m else None


def _archive_year(target_url: str, anchor_text: str) -> int | None:
    """年別アーカイブそのものの年をURLまたはアンカーから取得する。

    /2026/08/26/9330 のような通常記事URLは年別アーカイブとみなさない。
    """
    parsed = urlparse(target_url)
    path = parsed.path.lower().rstrip("/")
    query = parsed.query.lower()
    anchor = re.sub(r"\s+", "", anchor_text.lower())

    m = re.search(r"/(?:y)?(20\d{2})$", path)
    if m:
        return int(m.group(1))

    m = re.search(r"(?:^|&)(?:year|y)=(20\d{2})(?:&|$)", query)
    if m:
        return int(m.group(1))

    m = re.fullmatch(r"(20\d{2})年?", anchor)
    if m:
        return int(m.group(1))

    return None


def _is_archive_context_url(url: str) -> bool:
    """ニュース/展示会一覧または年別アーカイブかを判定する。"""
    lower = url.lower()
    if any(hint in lower for hint in _ARCHIVE_HINTS):
        return True
    path = urlparse(url).path.lower().rstrip("/")
    return bool(re.search(r"/(?:y)?20\d{2}(?:/page/\d+)?$", path))


def _archive_navigation_priority(current_url: str, target_url: str, anchor_text: str) -> int:
    """一覧ページから必要なページ送り/年別だけを優先する。

    WordPress等のページャに表示される 5, 10, 20, 30, 46 といった遠いページを
    一気にキューへ積むと、直近記事へ到達する前にページ枠を消費する。
    そのためページ送りは現在ページ+1のみ、年別は今年～2年前のみ許可する。
    """
    if not _is_archive_context_url(current_url):
        return 0

    anchor = re.sub(r"\s+", "", anchor_text.lower())
    current_page = _page_number(current_url) or 1
    target_page = _page_number(target_url)
    if target_page is not None:
        return 25 if target_page == current_page + 1 else 0

    # 明示的な「次へ」はURL形式が/page/NでないCMSもあるため許可する。
    if anchor in {">", "»", "next", "次へ", "次のページ"}:
        return 25

    year = _archive_year(target_url, anchor_text)
    if year is not None:
        current_year = date.today().year
        if year == current_year:
            return 90
        if year == current_year - 1:
            return 12
        if year == current_year - 2:
            return 8
        return 0

    return 0


def _is_archive_pagination(current_url: str, target_url: str, anchor_text: str) -> bool:
    """互換用。必要な一覧ナビゲーションだけTrueを返す。"""
    return _archive_navigation_priority(current_url, target_url, anchor_text) > 0


def _discover_candidate_links(html: str, current_url: str, site_url: str) -> list[tuple[int, str]]:
    """HTMLから同一サイト内の評価関連リンクとニュース一覧ページを抽出する。"""
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
        archive_priority = _archive_navigation_priority(current_url, canonical, anchor)
        if archive_priority:
            # アーカイブ移動は通常リンクスコアを上書きし、今年→詳細記事→前年の順に進みやすくする。
            score = archive_priority
        elif _page_number(canonical) is not None or _archive_year(canonical, anchor) is not None:
            # 遠いページ番号・古い年別リンクは、URLにnews/eventが含まれていても通常リンクとして拾わない。
            score = 0
        if score <= 0:
            continue
        if score > found.get(canonical, 0):
            found[canonical] = score

    return sorted(((score, url) for url, score in found.items()), reverse=True)


def crawl_site_sync(base_url: str, session: requests.Session, paths: list[str] | None = None) -> list[PageResult]:
    cfg = CRAWL_CFG
    results: list[PageResult] = []
    _paths = paths if paths else TARGET_PATHS

    seq = count()
    heap: list[tuple[int, int, str]] = []
    queued: set[str] = set()
    attempted: set[str] = set()
    successful_urls: set[str] = set()
    successful_html: set[str] = set()

    def enqueue(url: str, priority: int) -> None:
        canonical = _canonical_url(url)
        if canonical in queued or canonical in attempted:
            return
        queued.add(canonical)
        heapq.heappush(heap, (-priority, next(seq), canonical))

    enqueue(_normalize_url(base_url, "/"), 1000)

    # 展示会/ニュース一覧は専用シードとして先に確認する。
    # 404はresultsを消費しないため、存在しないパスが多い企業でもページ枠は減らない。
    for path in _ARCHIVE_SEED_PATHS:
        enqueue(_normalize_url(base_url, path), 650 + _link_score(path, path))

    for path in _paths:
        if path == "/":
            continue
        seed_url = _normalize_url(base_url, path)
        enqueue(seed_url, 200 + _link_score(seed_url, path))

    max_pages = cfg.max_pages_per_site + _ARCHIVE_EXTRA_PAGES
    max_attempts = max(max_pages * 8, 80)
    attempts = 0

    while heap and len(results) < max_pages and attempts < max_attempts:
        _, _, requested_url = heapq.heappop(heap)
        queued.discard(requested_url)
        if requested_url in attempted:
            continue
        attempted.add(requested_url)
        attempts += 1

        result = _fetch_page_sync(session, requested_url)
        if result.error:
            log.debug(f"  スキップ ({result.error}): {requested_url}")
            if len(attempted) == 1:
                break
            continue
        if result.status_code in (403, 404, 410):
            continue
        if result.status_code != 200 or not result.html:
            continue

        final_url = _canonical_url(result.url or requested_url)
        fp = _html_fingerprint(result.html)

        is_duplicate = final_url in successful_urls or fp in successful_html
        if not is_duplicate:
            result.url = final_url
            results.append(result)
            successful_urls.add(final_url)
            successful_html.add(fp)

        discovered = _discover_candidate_links(result.html, final_url, base_url)
        for score, discovered_url in discovered[:40]:
            # 実ページから見つかった関連リンクを固定シードより優先して深掘りする。
            enqueue(discovered_url, 300 + score)

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
