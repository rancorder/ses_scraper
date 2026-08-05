"""
parser/site_parser.py - HTML 構造解析
  ・崩れたHTMLでも動作（lxml + html.parser フォールバック）
  ・nav / h1 / h2 / footer / リンク抽出
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)


@dataclass
class ParsedPage:
    url:          str
    title:        str               = ""
    h1:           list[str]         = field(default_factory=list)
    h2:           list[str]         = field(default_factory=list)
    h3:           list[str]         = field(default_factory=list)
    nav_links:    list[str]         = field(default_factory=list)   # テキスト
    footer_text:  str               = ""
    body_text:    str               = ""
    internal_links: list[str]       = field(default_factory=list)
    emails:       list[str]         = field(default_factory=list)
    has_form:     bool              = False
    has_captcha:  bool              = False
    meta_desc:    str               = ""
    lang:         str               = ""


def _make_soup(html: str) -> BeautifulSoup:
    """lxmlで失敗したら html.parser にフォールバック"""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def parse_page(url: str, html: str) -> ParsedPage:
    """1ページのHTMLを解析してParsedPageを返す"""
    soup = _make_soup(html)
    domain = urlparse(url).netloc

    # 不要タグを除去
    for tag in soup(["script", "style", "noscript", "svg", "path"]):
        tag.decompose()

    result = ParsedPage(url=url)

    # タイトル
    title_tag = soup.find("title")
    result.title = title_tag.get_text(strip=True) if title_tag else ""

    # メタdescription
    meta = soup.find("meta", attrs={"name": "description"})
    if meta:
        result.meta_desc = meta.get("content", "")

    # 言語
    html_tag = soup.find("html")
    if html_tag:
        result.lang = html_tag.get("lang", "")

    # 見出し
    result.h1 = [t.get_text(strip=True) for t in soup.find_all("h1") if t.get_text(strip=True)]
    result.h2 = [t.get_text(strip=True) for t in soup.find_all("h2") if t.get_text(strip=True)]
    result.h3 = [t.get_text(strip=True) for t in soup.find_all("h3") if t.get_text(strip=True)]

    # ナビゲーション
    for nav in soup.find_all(["nav", "header"]):
        for a in nav.find_all("a"):
            txt = a.get_text(strip=True)
            if txt:
                result.nav_links.append(txt)

    # フッター
    footer = soup.find("footer")
    if footer:
        result.footer_text = footer.get_text(" ", strip=True)[:1000]

    # ボディ全テキスト（長すぎる場合は切り詰め）
    body = soup.find("body")
    if body:
        result.body_text = body.get_text(" ", strip=True)[:8000]

    # 内部リンク収集
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(url, href)
        parsed = urlparse(full)
        # 同一ドメインのみ
        if parsed.netloc == domain and full not in seen:
            seen.add(full)
            result.internal_links.append(full)

    # メールアドレス
    all_text = soup.get_text(" ")
    result.emails = list(set(_EMAIL_RE.findall(all_text)))[:5]

    # フォーム・CAPTCHA
    result.has_form = bool(soup.find("form"))
    captcha_markers = ["captcha", "recaptcha", "hcaptcha", "turnstile"]
    page_lower = html.lower()
    result.has_captcha = any(m in page_lower for m in captcha_markers)

    return result


def parse_pages(pages_html: list[tuple[str, str]]) -> list[ParsedPage]:
    """[(url, html), ...] を一括パース"""
    return [parse_page(url, html) for url, html in pages_html]
