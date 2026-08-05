"""
ses_list_scraper.py
SES企業一覧サイトから会社名を収集し、Google検索で公式URLを取得する
"""
import asyncio
import logging
import random
import time
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SES_LIST_URL = "https://ses-beginner.jp/ses-company-rankinglist/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.9",
}

EXCLUDE_DOMAINS = [
    "google.com", "google.co.jp", "youtube.com", "amazon.co.jp",
    "indeed.com", "doda.jp", "mynavi.jp", "recruit.co.jp",
    "wantedly.com", "linkedin.com", "facebook.com", "twitter.com",
    "wikipedia.org", "ses-beginner.jp",
]


def scrape_company_names() -> list[str]:
    """SES企業一覧サイトから会社名を取得"""
    try:
        r = requests.get(SES_LIST_URL, headers=HEADERS, timeout=15)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        
        names = []
        # テーブルから会社名を取得
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if cells:
                    name = cells[0].get_text(strip=True)
                    if name and name not in ["会社名", "資本金"] and len(name) > 1:
                        names.append(name)
        
        # 重複除去
        seen = set()
        unique = []
        for n in names:
            if n not in seen:
                seen.add(n)
                unique.append(n)
        
        log.info(f"会社名取得: {len(unique)}社")
        return unique
    except Exception as e:
        log.error(f"会社名取得エラー: {e}")
        return []


def search_company_url(company_name: str) -> str | None:
    """会社名でGoogle検索して公式URLを返す"""
    try:
        query = f"{company_name} 株式会社 公式サイト"
        url = f"https://www.google.com/search?q={requests.utils.quote(query)}&num=3"
        
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "lxml")
        
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Google検索結果のURLを抽出
            if href.startswith("/url?q="):
                actual_url = href.split("/url?q=")[1].split("&")[0]
                actual_url = requests.utils.unquote(actual_url)
                
                if not actual_url.startswith("http"):
                    continue
                    
                # 除外ドメインチェック
                if any(d in actual_url for d in EXCLUDE_DOMAINS):
                    continue
                    
                return actual_url.rstrip("/")
        
        return None
    except Exception as e:
        log.debug(f"URL検索エラー ({company_name}): {e}")
        return None


def scrape_ses_companies(
    max_companies: int = 0,
    delay_min: float = 1.0,
    delay_max: float = 3.0,
) -> list[dict]:
    """
    SES企業一覧から会社名を取得し、Google検索でURLを収集する
    
    Args:
        max_companies: 0=全件
        delay_min/max: リクエスト間隔（秒）
    """
    logging.basicConfig(level=logging.INFO)
    
    # 会社名取得
    names = scrape_company_names()
    if not names:
        return []
    
    if max_companies and max_companies < len(names):
        names = names[:max_companies]
    
    log.info(f"URL検索開始: {len(names)}社")
    
    companies = []
    for i, name in enumerate(names, 1):
        url = search_company_url(name)
        
        if url:
            companies.append({
                "name":    name,
                "url":     url,
                "source":  "SES企業一覧",
                "keyword": "SES",
            })
            log.info(f"[{i:4}/{len(names)}] ✓ {name} → {url}")
        else:
            log.info(f"[{i:4}/{len(names)}] - {name} (URL不明)")
        
        # レート制限
        if i < len(names):
            time.sleep(random.uniform(delay_min, delay_max))
    
    log.info(f"収集完了: {len(companies)}社（URL取得率: {len(companies)/len(names)*100:.1f}%）")
    return companies


if __name__ == "__main__":
    import json
    companies = scrape_ses_companies(max_companies=50)  # テスト用50社
    print(json.dumps(companies[:5], ensure_ascii=False, indent=2))
