"""
scraper/metoree.py - Metoree Playwright非同期スクレイパー
==========================================================
製造業特化プラットフォーム Metoree から企業URLを収集する。
https://metoree.com/
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urljoin, urlencode, urlparse, unquote

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright.async_api import TimeoutError as PWTimeout

log = logging.getLogger(__name__)

BASE_URL    = "https://metoree.com"
DELAY_MIN   = 1.2
DELAY_MAX   = 2.8
CONCURRENCY = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


@dataclass
class MetoreeCompany:
    ソース:         str = "Metoree"
    検索キーワード: str = ""
    会社名:         str = ""
    設立:           str = ""
    資本金:         str = ""
    従業員数:       str = ""
    住所:           str = ""
    電話:           str = ""
    公式サイト:     str = ""
    説明:           str = ""
    詳細URL:        str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_list_page(html: str, base_url: str) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    stubs = []
    seen = set()

    # 企業リンクを探す（複数セレクタを試みる）
    for a in soup.select("a[href*='/companies/'], a[href*='/company/']"):
        href = a.get("href", "")
        if not href:
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        # 一覧ページ自体のリンクを除外
        if re.search(r'/companies/\d+', full_url) or re.search(r'/company/[^/]+/?$', full_url):
            seen.add(full_url)
            name = a.get_text(strip=True)[:60] or full_url
            stubs.append({"name": name, "detail_url": full_url, "description": ""})

    return stubs


def _get_next_page_url(html: str, current_url: str) -> Optional[str]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    nxt = soup.select_one('a[rel="next"], a[class*="next"]')
    if nxt and nxt.get("href"):
        return urljoin(BASE_URL, nxt["href"])
    for a in soup.select("a"):
        txt = a.get_text(strip=True)
        if txt in ("次へ", "次のページ", "›", "»", ">", "NEXT"):
            href = a.get("href", "")
            if href and href != "#":
                return urljoin(BASE_URL, href)
    return None


def _parse_detail_page(html: str, detail_url: str) -> MetoreeCompany:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    c = MetoreeCompany(詳細URL=detail_url)

    # 会社名
    for sel in ["h1", "h2[class*='company']", "[class*='company-name']", "[class*='companyName']"]:
        t = soup.select_one(sel)
        if t:
            c.会社名 = t.get_text(strip=True)[:60]
            break

    # 公式サイト
    for a in soup.select("a[href^='http']"):
        href = a.get("href", "")
        txt  = a.get_text(strip=True)
        if any(kw in txt for kw in ["公式サイト", "ホームページ", "Webサイト", "公式HP", "企業サイト"]):
            if "metoree.com" not in href:
                c.公式サイト = href
                break
    if not c.公式サイト:
        # 外部リンクを探す
        for a in soup.select("a[href^='http']"):
            href = a.get("href", "")
            if "metoree.com" not in href and href.startswith("http"):
                c.公式サイト = href
                break

    # 説明
    for sel in ["[class*='description']", "[class*='about']", "p"]:
        t = soup.select_one(sel)
        if t and len(t.get_text(strip=True)) > 30:
            c.説明 = t.get_text(" ", strip=True)[:300]
            break

    # テーブル形式の会社情報
    for row in soup.select("tr, [class*='info-item'], [class*='company-info']"):
        txt = row.get_text(" ", strip=True)
        if "所在地" in txt or "住所" in txt:
            td = row.select_one("td:last-child, [class*='value'], [class*='data']")
            if td:
                c.住所 = td.get_text(strip=True)[:100]
        elif "電話" in txt or "TEL" in txt:
            td = row.select_one("td:last-child, [class*='value'], [class*='data']")
            if td:
                c.電話 = re.sub(r'[^\d\-\+\(\)]', '', td.get_text(strip=True))
        elif "従業員" in txt:
            td = row.select_one("td:last-child, [class*='value']")
            if td:
                c.従業員数 = td.get_text(strip=True)[:30]
        elif "設立" in txt:
            td = row.select_one("td:last-child, [class*='value']")
            if td:
                c.設立 = td.get_text(strip=True)[:20]

    return c


async def _fetch_html(page: Page, url: str, timeout_ms: int = 15000) -> Optional[str]:
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if resp is None or resp.status in (404, 403, 410):
            return None
        try:
            await page.wait_for_load_state("networkidle", timeout=2500)
        except PWTimeout:
            pass
        return await page.content()
    except PWTimeout:
        return None
    except Exception as e:
        log.debug(f"  取得エラー ({type(e).__name__}): {url}")
        return None


async def _scrape_keyword_async(
    keyword: str,
    context: BrowserContext,
    sem: asyncio.Semaphore,
    max_pages: Optional[int],
    stop_event: Optional[asyncio.Event],
) -> list[MetoreeCompany]:
    # Metoree検索URL
    search_url = f"{BASE_URL}/categories/search/?{urlencode({'keyword': keyword})}"
    page_num   = 0
    all_stubs: list[dict] = []
    seen_detail: set[str] = set()

    list_page = await context.new_page()
    try:
        page_url = search_url
        while page_url:
            if stop_event and stop_event.is_set():
                break
            if max_pages and page_num >= max_pages:
                break
            page_num += 1
            log.info(f"  [Metoree:{keyword}] 一覧 p{page_num}: {page_url}")
            html = await _fetch_html(list_page, page_url)
            if not html:
                break
            stubs = _parse_list_page(html, page_url)
            if not stubs:
                log.warning(f"  [Metoree:{keyword}] 企業リストなし → 終了")
                break
            for s in stubs:
                if s["detail_url"] not in seen_detail:
                    seen_detail.add(s["detail_url"])
                    all_stubs.append(s)
            next_url = _get_next_page_url(html, page_url)
            page_url = next_url if (next_url and next_url != page_url) else None
            if page_url:
                await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    finally:
        await list_page.close()

    if not all_stubs:
        return []

    log.info(f"  [Metoree:{keyword}] 詳細取得: {len(all_stubs)} 社 (並列数={CONCURRENCY})")

    companies = []
    done = 0

    async def _fetch_one(stub: dict) -> None:
        nonlocal done
        async with sem:
            try:
                page = await context.new_page()
            except Exception as e:
                log.warning(f"  詳細取得失敗: {stub['detail_url']} → {e}")
                return
            try:
                html = await _fetch_html(page, stub["detail_url"])
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

            if not html:
                c = MetoreeCompany(
                    会社名=stub["name"],
                    説明=stub.get("description", ""),
                    詳細URL=stub["detail_url"],
                )
            else:
                c = _parse_detail_page(html, stub["detail_url"])
                if not c.会社名:
                    c.会社名 = stub["name"]
                if not c.説明:
                    c.説明 = stub.get("description", "")
            c.検索キーワード = keyword
            companies.append(c)
            done += 1
            if done % 20 == 0 or done == len(all_stubs):
                log.info(f"  [Metoree:{keyword}] 詳細取得 {done}/{len(all_stubs)} 社完了")
            await asyncio.sleep(random.uniform(0.3, 0.8))

    BATCH = 50
    for batch_start in range(0, len(all_stubs), BATCH):
        batch = all_stubs[batch_start:batch_start + BATCH]
        await asyncio.gather(*[_fetch_one(s) for s in batch], return_exceptions=True)

    return companies


async def scrape_metoree_async(
    keywords: list[str],
    max_pages: Optional[int] = 5,
    stop_event: Optional[asyncio.Event] = None,
    concurrency: int = CONCURRENCY,
) -> list[MetoreeCompany]:
    sem = asyncio.Semaphore(concurrency)
    all_companies: list[MetoreeCompany] = []
    seen_urls: set[str] = set()

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu", "--no-zygote", "--single-process",
            ],
        )
        context: BrowserContext = await browser.new_context(
            user_agent=USER_AGENT, locale="ja-JP", timezone_id="Asia/Tokyo",
            ignore_https_errors=True,
            extra_http_headers={"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        try:
            for keyword in keywords:
                if stop_event and stop_event.is_set():
                    break
                companies = await _scrape_keyword_async(keyword, context, sem, max_pages, stop_event)
                added = 0
                for c in companies:
                    key = c.公式サイト or c.詳細URL or c.会社名
                    if key and key not in seen_urls:
                        seen_urls.add(key)
                        all_companies.append(c)
                        added += 1
                log.info(f"  [Metoree:{keyword}] +{added}社（累計: {len(all_companies)}）")
        finally:
            await context.close()
            await browser.close()

    log.info(f"\n[Metoree] 完了: 合計 {len(all_companies)} 社")
    return all_companies
