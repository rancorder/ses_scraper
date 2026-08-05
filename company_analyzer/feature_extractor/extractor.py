"""
feature_extractor/extractor.py - 特徴量抽出エンジン
  ParsedPage のリスト → SiteFeatures
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from company_analyzer.config import (
    TECH_KEYWORDS, OEM_KEYWORDS, PRODUCT_SIGNALS,
    RECRUITMENT_AI_KEYWORDS, RECRUITMENT_ENG_KEYWORDS,
)
from company_analyzer.models import SiteFeatures
from company_analyzer.parser.site_parser import ParsedPage

log = logging.getLogger(__name__)

# 型番パターン: 英数字+ハイフン+数字 の組み合わせ
_PART_NUMBER_RE = re.compile(
    r"\b[A-Z]{1,5}[-_]?\d{2,}[-_]?[A-Z0-9]*\b"
)
# spec表記（数値+単位）
_SPEC_RE = re.compile(
    r"\d+\.?\d*\s*(MHz|GHz|MB|GB|mA|mW|W|V|ms|us|ns|°C|rpm|dB|fps|bps|kbps|Mbps|Gbps)",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """テキストを小文字化・正規化"""
    return text.lower().replace("\u3000", " ").replace("\n", " ")


def _search_keywords(text: str, keywords: list[str]) -> list[str]:
    """テキスト中にヒットするキーワードのリストを返す"""
    norm = _normalize(text)
    return [kw for kw in keywords if kw.lower() in norm]


def _has_product_signals(pages: list[ParsedPage]) -> tuple[bool, list[str]]:
    """
    製品ページ存在の判定：
    - /products /product を含むURLが存在
    - 型番・スペック表記が検出される
    - PRODUCT_SIGNALS キーワードがヒット
    """
    product_urls = []
    found_signals = False

    for page in pages:
        path = urlparse(page.url).path.lower()
        is_product_path = any(
            seg in path for seg in ["/product", "/lineup", "/solution", "/technology"]
        )

        all_text = " ".join([
            page.title, " ".join(page.h1), " ".join(page.h2),
            page.body_text, page.meta_desc,
        ])

        has_part_no   = bool(_PART_NUMBER_RE.search(page.body_text))
        has_spec      = bool(_SPEC_RE.search(page.body_text))
        hit_signals   = _search_keywords(all_text, PRODUCT_SIGNALS)

        if is_product_path and (has_part_no or has_spec or len(hit_signals) >= 2):
            product_urls.append(page.url)
            found_signals = True
        elif len(hit_signals) >= 3:
            found_signals = True

    return found_signals, product_urls


def _has_oem_signals(pages: list[ParsedPage]) -> tuple[bool, list[str]]:
    """OEM / 共同開発文化の検出"""
    oem_urls = []

    for page in pages:
        all_text = " ".join([
            page.title, " ".join(page.h1), " ".join(page.h2),
            page.body_text, page.footer_text,
        ])
        hits = _search_keywords(all_text, OEM_KEYWORDS)
        if len(hits) >= 2:
            oem_urls.append(page.url)

    return bool(oem_urls), oem_urls


def _extract_tech_keywords(pages: list[ParsedPage]) -> dict[str, list[str]]:
    """カテゴリ別に技術キーワードを抽出"""
    all_text = " ".join(
        " ".join([p.title, *p.h1, *p.h2, p.body_text[:3000]])
        for p in pages
    )

    result: dict[str, list[str]] = {}
    for category, keywords in TECH_KEYWORDS.items():
        hits = _search_keywords(all_text, keywords)
        if hits:
            result[category] = list(set(hits))

    return result


def _detect_recruitment(pages: list[ParsedPage]) -> tuple[bool, bool]:
    """採用ページからAI/組み込みエンジニアの募集を検出"""
    recruit_pages = [
        p for p in pages
        if any(
            seg in urlparse(p.url).path.lower()
            for seg in ["/recruit", "/career", "/job", "/採用"]
        )
    ]
    # 採用ページがなければ全ページを対象に
    if not recruit_pages:
        recruit_pages = pages

    all_text = " ".join(p.body_text[:2000] for p in recruit_pages)

    has_ai_recruit  = bool(_search_keywords(all_text, RECRUITMENT_AI_KEYWORDS))
    has_eng_recruit = bool(_search_keywords(all_text, RECRUITMENT_ENG_KEYWORDS))

    return has_ai_recruit, has_eng_recruit


def _detect_tech_stack(pages: list[ParsedPage]) -> dict[str, str]:
    """
    技術スタック検出（将来拡張用）
    HTMLコメント・metaタグ・スクリプトタグから検出
    """
    stack: dict[str, str] = {}
    for page in pages:
        text_lower = page.body_text.lower()
        # 検出可能なフレームワーク・ライブラリ
        detections = {
            "react":      "React",
            "vue":        "Vue.js",
            "angular":    "Angular",
            "next.js":    "Next.js",
            "wordpress":  "WordPress",
            "shopify":    "Shopify",
        }
        for marker, name in detections.items():
            if marker in text_lower:
                stack["frontend"] = name
                break

    return stack


def _extract_contact(pages: list[ParsedPage]) -> tuple[str | None, bool, bool]:
    """メールアドレス・フォーム・CAPTCHA の検出"""
    for page in pages:
        email = page.emails[0] if page.emails else None
        if email:
            return email, page.has_form, page.has_captcha

    # メールがなければフォームだけチェック
    has_form    = any(p.has_form for p in pages)
    has_captcha = any(p.has_captcha for p in pages)
    return None, has_form, has_captcha


def _detect_site_activity(pages: list[ParsedPage]) -> tuple[bool, bool, bool]:
    """ニュース・ブログ・プレスリリースの検出"""
    urls_lower = [urlparse(p.url).path.lower() for p in pages]
    all_text = " ".join(p.title + p.body_text[:500] for p in pages).lower()

    has_news  = any("/news" in u or "ニュース" in u for u in urls_lower) or "news" in all_text
    has_blog  = any("/blog" in u for u in urls_lower) or "blog" in all_text
    has_press = any("/press" in u or "/ir" in u for u in urls_lower)

    return has_news, has_blog, has_press


def extract_features(domain: str, pages: list[ParsedPage]) -> SiteFeatures:
    """ParsedPage リスト → SiteFeatures"""
    if not pages:
        return SiteFeatures(domain=domain)

    product_found, product_urls   = _has_product_signals(pages)
    oem_found,     oem_urls       = _has_oem_signals(pages)
    tech_kw                       = _extract_tech_keywords(pages)
    has_ai_rec,    has_eng_rec    = _detect_recruitment(pages)
    email, has_form, has_captcha  = _extract_contact(pages)
    has_news, has_blog, has_press = _detect_site_activity(pages)
    tech_stack                    = _detect_tech_stack(pages)

    return SiteFeatures(
        domain           = domain,
        pages_crawled    = [p.url for p in pages],
        product_presence = product_found,
        oem_presence     = oem_found,
        tech_keywords    = tech_kw,
        recruitment_ai   = has_ai_rec,
        recruitment_eng  = has_eng_rec,
        contact_email    = email,
        has_contact_form = has_form,
        has_captcha      = has_captcha,
        has_news         = has_news,
        has_blog         = has_blog,
        has_press        = has_press,
        product_pages    = product_urls,
        oem_pages        = oem_urls,
        tech_stack       = tech_stack,
    )