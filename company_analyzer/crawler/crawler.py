"""
crawler/crawler.py - requests 軽量クローラー（Playwright版から置き換え）
========================================================================
企業公式サイトはほぼ静的HTMLのため requests + BeautifulSoup で十分。
メモリ使用量: Playwright版の約1/20
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from urllib.parse import urljoin, urlparse

import requests
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

TIMEOUT = 12  # 1社あたり最大12秒


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


def _normalize_url(base: str, path: str) -> str:
    if path.startswith("http"):
        return path
    return urljoin(base, path)


def _fetch_page_sync(session: requests.Session, url: str) -> PageResult:
    """1ページを同期的に取得"""
    start = time.monotonic()
    try:
        resp = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code in (404, 410, 403):
            return PageResult(url=url, status_code=resp.status_code)

        # エンコーディング自動検出
        if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"

        html = resp.text
        return PageResult(url=url, status_code=resp.status_code, html=html, elapsed_ms=elapsed_ms)

    except requests.exceptions.Timeout:
        return PageResult(url=url, error="timeout")
    except requests.exceptions.SSLError:
        # SSL失敗時はverify=Falseで再試行
        try:
            resp = session.get(url, timeout=TIMEOUT, allow_redirects=True, verify=False)
            if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding or "utf-8"
            return PageResult(url=url, status_code=resp.status_code, html=resp.text,
                            elapsed_ms=int((time.monotonic() - start) * 1000))
        except Exception as e2:
            return PageResult(url=url, error=f"ssl_error: {str(e2)[:60]}")
    except Exception as e:
        return PageResult(url=url, error=f"{type(e).__name__}: {str(e)[:60]}")


def crawl_site_sync(base_url: str, session: requests.Session, paths: list[str] | None = None) -> list[PageResult]:
    """1社分のページを同期クロール"""
    cfg = CRAWL_CFG
    results: list[PageResult] = []
    crawled = 0
    _paths = paths if paths else TARGET_PATHS

    for path in _paths:
        if crawled >= cfg.max_pages_per_site:
            break

        url = _normalize_url(base_url, path)
        result = _fetch_page_sync(session, url)

        if result.error:
            log.debug(f"  スキップ ({result.error}): {url}")
            if path == "/":
                break  # トップが取れなければ以降スキップ
            continue

        if result.status_code == 404:
            continue

        if result.status_code == 200 and result.html:
            results.append(result)
            crawled += 1

    # ドメイン間の最小遅延
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
    全企業を並列クロール（requests版・スレッドプール使用）
    paths: 巡回するパスのリスト。Noneの場合はconfig.TARGET_PATHSを使用。
    """
    cfg = CRAWL_CFG
    _paths = paths if paths else TARGET_PATHS
    max_workers = min(concurrency or cfg.concurrency, 8)  # 最大8並列
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

    tasks = [
        _crawl_one(url, name)
        for url, name in url_to_name.items()
    ]
    await asyncio.gather(*tasks)

    log.info(f"  クロール完了: {total}社 / エラー: {error_count}社")
    return results