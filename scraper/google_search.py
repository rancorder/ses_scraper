"""
scraper/google_search.py - Google検索結果から企業URLを収集
===========================================================
PlaywrightでGoogle検索結果ページをスクレイピングし、
企業サイトのURLと会社名を抽出する。

・ブロック対策: ランダムUA・遅延・複数検索クエリに分散
・IPROSと同じ形式（name, url, 住所, 電話）で返す
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Optional
from urllib.parse import urlparse, urlencode, quote_plus

from playwright.async_api import async_playwright, BrowserContext, Page
from playwright.async_api import TimeoutError as PWTimeout

log = logging.getLogger(__name__)

# ── 除外ドメイン（広告・ポータル・SNS等） ──────────────────────────
EXCLUDE_DOMAINS = {
    "google.com", "google.co.jp", "youtube.com", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "wikipedia.org", "amazon.co.jp", "amazon.com", "rakuten.co.jp",
    "ipros.jp", "mono.ipros.com", "salesnow.jp",
    "indeed.com", "recruit.co.jp", "mynavi.jp", "rikunabi.com",
    "doda.jp", "en-japan.com", "wantedly.com",
    "nikkei.com", "asahi.com", "yomiuri.co.jp", "mainichi.jp",
    "pref.", "city.", "go.jp", "ac.jp",
    "maps.app.goo.gl", "goo.gl",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


def _is_company_url(url: str) -> bool:
    """企業サイトらしいURLかどうか判定"""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")
        # 除外ドメインチェック
        for excl in EXCLUDE_DOMAINS:
            if excl in domain:
                return False
        # httpで始まるURLのみ
        if not url.startswith("http"):
            return False
        # .co.jp / .jp / .com などの企業ドメインを優先
        return True
    except Exception:
        return False


def _extract_company_name(title: str, url: str) -> str:
    """タイトルから会社名を抽出"""
    # 「株式会社〇〇 | 〇〇ページ」→「株式会社〇〇」
    for sep in [" | ", " - ", " – ", " ｜ ", "｜", " / "]:
        if sep in title:
            parts = title.split(sep)
            # 会社名らしい部分を優先
            for part in parts:
                p = part.strip()
                if any(kw in p for kw in ["株式会社", "有限会社", "合同会社", "合資会社", "㈱", "㈲"]):
                    return p
            return parts[0].strip()
    return title.strip()[:50]


async def _fetch_google_page(
    page: Page,
    query: str,
    page_num: int = 1,
) -> Optional[str]:
    """Google検索結果ページを取得"""
    start = (page_num - 1) * 10
    params = {"q": query, "start": str(start), "hl": "ja", "gl": "jp", "num": "10"}
    url = f"https://www.google.com/search?{urlencode(params)}"

    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        if resp is None or resp.status in (429, 403):
            log.warning(f"  Google検索ブロック検知 (status={resp.status if resp else 'None'}): {query}")
            return None
        await asyncio.sleep(random.uniform(1.5, 3.0))
        return await page.content()
    except PWTimeout:
        log.warning(f"  Google検索タイムアウト: {query}")
        return None
    except Exception as e:
        log.warning(f"  Google検索エラー: {e}")
        return None


def _parse_google_results(html: str) -> list[dict]:
    """Google検索結果HTMLから企業URL・タイトルを抽出"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    results = []

    # Google検索結果の標準セレクタ
    for g in soup.select("div.g, div[data-sokoban-container]"):
        # タイトルとURL
        a = g.select_one("a[href]")
        if not a:
            continue
        href = a.get("href", "")
        if not href.startswith("http"):
            continue

        title_el = g.select_one("h3")
        title = title_el.get_text(strip=True) if title_el else ""

        if _is_company_url(href) and title:
            results.append({"url": href, "title": title})

    # 重複除去
    seen = set()
    unique = []
    for r in results:
        domain = urlparse(r["url"]).netloc
        if domain not in seen:
            seen.add(domain)
            unique.append(r)

    return unique


async def search_google_async(
    queries: list[str],
    max_pages: int = 3,
    suffix: str = "会社 企業",
    stop_event: Optional[asyncio.Event] = None,
) -> list[dict]:
    """
    Google検索で企業リストを収集する。

    Args:
        queries:    検索キーワードリスト
        max_pages:  1キーワードあたりの検索ページ数（1ページ=10件）
        suffix:     クエリに自動付加するサフィックス
        stop_event: 停止シグナル

    Returns:
        [{"name": ..., "url": ..., "住所": "", "電話": "", "keyword": ...}, ...]
    """
    all_results: list[dict] = []
    seen_domains: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu", "--no-zygote", "--single-process",
            ],
        )
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        page = await context.new_page()

        try:
            for kw in queries:
                if stop_event and stop_event.is_set():
                    break

                query = f"{kw} {suffix}".strip()
                kw_count = 0
                log.info(f"  [Google] 検索: 「{query}」")

                for pg in range(1, max_pages + 1):
                    if stop_event and stop_event.is_set():
                        break

                    html = await _fetch_google_page(page, query, pg)
                    if not html:
                        break

                    # Captcha検知
                    if "unusual traffic" in html.lower() or "captcha" in html.lower():
                        log.warning(f"  [Google] CAPTCHA検知 → このクエリをスキップ: {query}")
                        break

                    items = _parse_google_results(html)
                    if not items:
                        log.info(f"  [Google] 「{query}」p{pg}: 結果なし → 終了")
                        break

                    added = 0
                    for item in items:
                        domain = urlparse(item["url"]).netloc
                        if domain not in seen_domains:
                            seen_domains.add(domain)
                            name = _extract_company_name(item["title"], item["url"])
                            all_results.append({
                                "name":    name,
                                "url":     item["url"],
                                "住所":    "",
                                "電話":    "",
                                "keyword": kw,
                                "source":  "Google",
                            })
                            added += 1
                            kw_count += 1

                    log.info(f"  [Google] 「{query}」p{pg}: +{added}件（累計: {len(all_results)}社）")

                    # ページ間の遅延（Botブロック対策）
                    await asyncio.sleep(random.uniform(2.0, 4.0))

                log.info(f"  [Google] 「{kw}」完了: {kw_count}社")

        finally:
            await page.close()
            await context.close()
            await browser.close()

    log.info(f"\n[Google] 合計 {len(all_results)} 社収集")
    return all_results
