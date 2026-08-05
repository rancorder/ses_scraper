"""
scraper/ipros.py - IPROS Playwright非同期並列スクレイパー
==========================================================
元の ipros_scraper.py のHTML解析ロジックをそのまま流用し、
HTTPアクセス部分のみ Playwright に置き換えた版。

主な改善点:
  ・詳細ページを asyncio.Semaphore で並列取得（デフォルト5並列）
  ・JS動的コンテンツ対応
  ・Ctrl+C で途中保存して安全終了
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urljoin, urlencode, urlparse, parse_qs, urlunsplit, unquote

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright.async_api import TimeoutError as PWTimeout

log = logging.getLogger(__name__)

BASE_URL    = "https://mono.ipros.com"
DELAY_MIN   = 1.2
DELAY_MAX   = 2.8
CONCURRENCY = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


@dataclass
class IprosCompany:
    ソース:         str = "IPROS"
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


# ─── HTML解析 ──────────────────────────────────────────────────────────

def _parse_list_page(html: str) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    items = soup.select(
        "section[class*='search-result-company-item'], "
        "div[class*='search-result-company-item']:not([class*='_'])"
    )
    if not items:
        items = soup.find_all(
            lambda tag: tag.name in ("section", "div", "li")
            and any("search-result-company-item" in c for c in tag.get("class", []))
            and not any(c.endswith("_bookmark") for c in tag.get("class", []))
        )
    stubs = []
    for item in items:
        link = (
            item.select_one("a[class*='company-item_name']")
            or item.select_one("a[class*='company_name']")
            or item.select_one("a[href*='/company/']")
        )
        if not link:
            continue
        href = link.get("href", "")
        if not href:
            continue
        desc_tag = item.select_one("[class*='_description']")
        stubs.append({
            "name":        link.get_text(strip=True),
            "detail_url":  urljoin(BASE_URL, href),
            "description": desc_tag.get_text(strip=True) if desc_tag else "",
        })
    return stubs


def _get_next_page_url(html: str, current_url: str) -> Optional[str]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    nxt = soup.select_one('a[rel="next"], a[class*="next"]')
    if nxt and nxt.get("href"):
        return urljoin(BASE_URL, nxt["href"])
    for a in soup.select("a"):
        if a.get_text(strip=True) in ("次へ", "次のページ", "›", "»", ">", "NEXT"):
            href = a.get("href", "")
            if href and href != "#":
                return urljoin(BASE_URL, href)
    pager = soup.select_one('[class*="pager"], [class*="pagination"]')
    if pager:
        active = pager.select_one('[class*="current"], [class*="active"], strong')
        if active:
            try:
                cur = int(re.sub(r"\D", "", active.get_text()))
                nxt_p = cur + 1
                parsed = urlparse(current_url)
                qs = parse_qs(parsed.query)
                qs["page"] = [str(nxt_p)]
                new_q = urlencode({k: v[0] for k, v in qs.items()})
                nxt_url = urlunsplit(parsed._replace(query=new_q))
                links = [a.get("href", "") for a in pager.select("a")]
                if any(f"page={nxt_p}" in h for h in links):
                    return nxt_url
            except (ValueError, TypeError):
                pass
    return None


def _resolve_official_url(href: str) -> str:
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if "url" in qs:
            raw = unquote(qs["url"][0])
            if raw.startswith("//"):
                return "https:" + raw
            if not raw.startswith("http"):
                return "https://" + raw
            return raw
    except Exception:
        pass
    return urljoin(BASE_URL, href)


def _parse_detail_page(html: str, detail_url: str) -> IprosCompany:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    c = IprosCompany(詳細URL=detail_url)

    name_tag = (
        soup.select_one("h2[class*='company-summary__catch']")
        or soup.select_one("h2[class*='company-summary']")
        or soup.select_one("h1[class*='company']")
    )
    if name_tag:
        c.会社名 = name_tag.get_text(strip=True)

    for item in soup.select(
        "[class*='company-summary__description-item'], [class*='company-info__item']"
    ):
        lbl_el = item.select_one("[class*='description-label'], [class*='__label']")
        if not lbl_el:
            continue
        lbl = lbl_el.get_text(strip=True)
        if "電話" in lbl:
            tel = item.select_one("a[href^='tel:'], [class*='description-tell']")
            if tel:
                h = tel.get("href", "")
                c.電話 = h.replace("tel:", "").strip() if h.startswith("tel:") else tel.get_text(strip=True)
            continue
        val_el = item.select_one("[class*='description-secondary'], [class*='__secondary']")
        if not val_el:
            continue
        val = val_el.get_text(strip=True)
        if "設立" in lbl:              c.設立 = val
        elif "資本金" in lbl:          c.資本金 = val
        elif "従業員" in lbl:          c.従業員数 = val
        elif "住所" in lbl or "所在地" in lbl: c.住所 = val

    if not c.電話:
        tel = soup.select_one("a[href^='tel:']")
        if tel:
            c.電話 = tel.get("href", "").replace("tel:", "").strip()

    official = None
    for span in soup.select("span[class*='text-button__label']"):
        if "公式サイト" in span.get_text(strip=True):
            official = span.find_parent("a")
            break
    if not official:
        for a in soup.select("a"):
            if a.get_text(strip=True) in ("公式サイト", "公式サイトを見る", "Webサイト"):
                official = a
                break
    if not official:
        official = soup.select_one("a[href*='oreConversion'], a[href*='externalLink']")
    if official:
        raw = official.get("href", "")
        if raw:
            c.公式サイト = _resolve_official_url(raw)

    for sel in [".company-summary__description", ".pr-text", ".about"]:
        t = soup.select_one(sel)
        if t:
            c.説明 = t.get_text(" ", strip=True)[:300]
            break

    return c


# ─── Playwright非同期フェッチ ──────────────────────────────────────────

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


async def _fetch_detail(
    context: BrowserContext,
    sem: asyncio.Semaphore,
    stub: dict,
    keyword: str,
    delay: float,
) -> Optional[IprosCompany]:
    await asyncio.sleep(delay)
    async with sem:
        # context が閉じられていたら安全にスキップ
        try:
            page = await context.new_page()
        except Exception as e:
            log.warning(f"  詳細取得失敗: {stub['detail_url']} → {e}")
            c = IprosCompany(会社名=stub["name"], 説明=stub.get("description", ""), 詳細URL=stub["detail_url"])
            c.検索キーワード = keyword
            return c
        try:
            html = await _fetch_html(page, stub["detail_url"])
        finally:
            try:
                await page.close()
            except Exception:
                pass
    if not html:
        c = IprosCompany(会社名=stub["name"], 説明=stub.get("description", ""), 詳細URL=stub["detail_url"])
    else:
        c = _parse_detail_page(html, stub["detail_url"])
        if not c.会社名: c.会社名 = stub["name"]
        if not c.説明:   c.説明   = stub.get("description", "")
    c.検索キーワード = keyword
    return c


async def _scrape_keyword_async(
    keyword: str,
    context: BrowserContext,
    sem: asyncio.Semaphore,
    max_pages: Optional[int],
    stop_event: Optional[asyncio.Event],
) -> list[IprosCompany]:
    page_url  = f"{BASE_URL}/search/company/?{urlencode({'l': '60', 'keyword': keyword})}"
    page_num  = 0
    all_stubs: list[dict] = []
    seen_detail: set[str] = set()

    list_page = await context.new_page()
    try:
        while page_url:
            if stop_event and stop_event.is_set():
                break
            if max_pages and page_num >= max_pages:
                break
            page_num += 1
            log.info(f"  [IPROS:{keyword}] 一覧 p{page_num}: {page_url}")
            html = await _fetch_html(list_page, page_url)
            if not html:
                break
            stubs = _parse_list_page(html)
            if not stubs:
                log.warning(f"  [IPROS:{keyword}] 企業リストなし → 終了")
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

    log.info(f"  [IPROS:{keyword}] 詳細並列取得: {len(all_stubs)} 社 (並列数={CONCURRENCY})")

    companies = []
    done = 0

    async def _fetch_one(stub: dict) -> None:
        nonlocal done
        async with sem:
            # context が閉じられていたら安全にスキップ
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
                c = IprosCompany(
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
                log.info(f"  [IPROS:{keyword}] 詳細取得 {done}/{len(all_stubs)} 社完了")
            # 短いランダム遅延（サーバー負荷軽減）
            await asyncio.sleep(random.uniform(0.3, 0.8))

    # バッチ処理（50社ずつ）
    BATCH = 50
    for batch_start in range(0, len(all_stubs), BATCH):
        batch = all_stubs[batch_start:batch_start + BATCH]
        await asyncio.gather(*[_fetch_one(s) for s in batch], return_exceptions=True)

    return companies


async def scrape_ipros_async(
    keywords: list[str],
    max_pages: Optional[int] = 5,
    stop_event: Optional[asyncio.Event] = None,
    concurrency: int = CONCURRENCY,
) -> list[IprosCompany]:
    sem = asyncio.Semaphore(concurrency)
    all_companies: list[IprosCompany] = []
    seen_urls: set[str] = set()

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
                "--no-zygote",
                "--disable-setuid-sandbox",
                "--memory-pressure-off",
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
                    log.warning("⛔ 停止シグナル受信 (IPROS)")
                    break
                companies = await _scrape_keyword_async(keyword, context, sem, max_pages, stop_event)
                added = 0
                for c in companies:
                    if c.詳細URL and c.詳細URL not in seen_urls:
                        seen_urls.add(c.詳細URL)
                        all_companies.append(c)
                        added += 1
                log.info(f"  [IPROS:{keyword}] +{added} 社追加（累計: {len(all_companies)}）")
        finally:
            await context.close()
            await browser.close()

    log.info(f"\n[IPROS] 完了: 合計 {len(all_companies)} 社")
    return all_companies


def scrape_ipros(keywords, max_pages=5, stop_event=None, concurrency=CONCURRENCY):
    """同期ラッパー"""
    async def _run():
        async_stop = asyncio.Event()
        if stop_event:
            async def _watch():
                while not async_stop.is_set():
                    if stop_event.is_set():
                        async_stop.set()
                    await asyncio.sleep(0.5)
            asyncio.create_task(_watch())
        return await scrape_ipros_async(keywords, max_pages, async_stop if stop_event else None, concurrency)
    return asyncio.run(_run())
