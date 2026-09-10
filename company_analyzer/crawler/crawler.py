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
_COMPANY_SCOPE_SEED_PATHS = (
    "/about/locations",
    "/about/group",
    "/company/group",
    "/corporate/group",
    "/group",
)
_ARCHIVE_TRIGGER_RE = re.compile(
    r"出展|展示会|見本市|フェア|ブース|exhibition|expo|trade\s*show|新製品|新商品",
    re.IGNORECASE,
)
_LEGAL_FORM_RE = re.compile(
    r"(?:株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|一般財団法人|\(株\)|（株）|㈱|"
    r"inc\.?|co\.?\s*,?\s*ltd\.?|ltd\.?)",
    re.IGNORECASE,
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


def _origin_root_url(url: str) -> str:
    """個別ページURLから同一ドメインのルートURLを作る。"""
    p = urlparse(url)
    return urlunparse((p.scheme.lower(), p.netloc.lower(), "/", "", "", ""))


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


def _company_aliases_for_link(company_name: str) -> list[str]:
    raw = str(company_name or "").strip()
    if not raw:
        return []
    stripped = _LEGAL_FORM_RE.sub("", raw).strip()
    aliases = [raw, stripped]
    result: list[str] = []
    for value in aliases:
        compact = re.sub(r"[\s\u3000・･\-‐‑–—_/|｜()（）\[\]【】]", "", value.lower())
        if len(compact) >= 3 and compact not in result:
            result.append(compact)
    return result


def _target_company_link(anchor_text: str, company_name: str) -> bool:
    if not company_name:
        return False
    anchor = re.sub(
        r"[\s\u3000・･\-‐‑–—_/|｜()（）\[\]【】]",
        "",
        str(anchor_text or "").lower(),
    )
    return any(alias in anchor for alias in _company_aliases_for_link(company_name))


def _page_number(url: str) -> int | None:
    path = urlparse(url).path.lower().rstrip("/")
    m = re.search(r"/page/(\d+)$", path)
    return int(m.group(1)) if m else None


def _archive_year(target_url: str, anchor_text: str) -> int | None:
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
    lower = url.lower()
    if any(hint in lower for hint in _ARCHIVE_HINTS):
        return True
    path = urlparse(url).path.lower().rstrip("/")
    return bool(re.search(r"/(?:y)?20\d{2}(?:/page/\d+)?$", path))


def _archive_navigation_priority(current_url: str, target_url: str, anchor_text: str) -> int:
    if not _is_archive_context_url(current_url):
        return 0

    anchor = re.sub(r"\s+", "", anchor_text.lower())
    current_page = _page_number(current_url) or 1
    target_page = _page_number(target_url)
    if target_page is not None:
        if target_page != current_page + 1:
            return 0
        if target_page == 2:
            return 150
        if target_page == 3:
            return 55
        return 0

    if anchor in {">", "»", "next", "次へ", "次のページ"}:
        if current_page == 1:
            return 150
        if current_page == 2:
            return 55
        return 0

    year = _archive_year(target_url, anchor_text)
    if year is not None:
        current_year = date.today().year
        if year == current_year:
            return 140
        if year == current_year - 1:
            return 18
        if year == current_year - 2:
            return 12
        return 0

    return 0


def _is_archive_pagination(current_url: str, target_url: str, anchor_text: str) -> bool:
    return _archive_navigation_priority(current_url, target_url, anchor_text) > 0


def _discover_candidate_links(
    html: str,
    current_url: str,
    site_url: str,
    company_name: str = "",
) -> list[tuple[int, str]]:
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    site_host = _host_key(site_url)
    archive_context = _is_archive_context_url(current_url)
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
        anchor = a.get_text(" ", strip=True)[:160]
        score = _link_score(canonical, anchor)
        archive_priority = _archive_navigation_priority(current_url, canonical, anchor)
        if archive_priority:
            score = archive_priority
        elif _page_number(canonical) is not None or _archive_year(canonical, anchor) is not None:
            score = 0
        elif archive_context and _ARCHIVE_TRIGGER_RE.search(anchor):
            score = max(score, 170)

        # グループサイトでは対象会社名そのもののリンクを最優先する。
        if _target_company_link(anchor, company_name):
            score = max(score, 600)

        if score <= 0:
            continue
        if score > found.get(canonical, 0):
            found[canonical] = score

    return sorted(((score, url) for url, score in found.items()), reverse=True)


def crawl_site_sync(
    base_url: str,
    session: requests.Session,
    paths: list[str] | None = None,
    company_name: str = "",
) -> list[PageResult]:
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

    # 入力URLそのものを最優先で取得する。
    enqueue(base_url, 1000)

    # 入力が個別ページでも、ドメインルートを必ず確認する。
    # 親会社・グループ共通サイトか、自社専用サイトかの判定に使用する。
    origin_root = _origin_root_url(base_url)
    if _canonical_url(origin_root) != _canonical_url(base_url):
        enqueue(origin_root, 990)

    # 親会社/グループ共通ドメインから対象会社固有ページを見つけるための入口。
    for path in _COMPANY_SCOPE_SEED_PATHS:
        enqueue(_normalize_url(base_url, path), 850 + _link_score(path, path))

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

        discovered = _discover_candidate_links(
            result.html,
            final_url,
            base_url,
            company_name=company_name,
        )
        for score, discovered_url in discovered[:40]:
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
                    None, crawl_site_sync, url, session, _paths, name
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
