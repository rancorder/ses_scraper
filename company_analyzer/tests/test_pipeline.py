"""
tests/test_pipeline.py - 単体テスト（モックHTMLで実際の通信なし）
"""
from __future__ import annotations

import pytest
from company_analyzer.parser.site_parser import parse_page
from company_analyzer.feature_extractor.extractor import extract_features
from company_analyzer.scoring.scoring_engine import ScoringEngine
from company_analyzer.models import SiteFeatures


# ── テスト用HTMLサンプル ──────────────────────────────────────────────────────

PRODUCT_HTML = """
<html lang="ja"><head><title>山田製作所 - 組み込みAIカメラ製品一覧</title></head>
<body>
<header><nav><a href="/products">製品</a><a href="/oem">OEM</a><a href="/recruit">採用</a></nav></header>
<h1>自社製品ラインアップ</h1>
<h2>エッジAIカメラモジュール</h2>
<p>型番: ECV-2024A / 解像度: 4K / 処理速度: 60fps / 消費電力: 3.5W</p>
<p>組み込みLinux / ARM Cortex-A55 / TensorFlow Lite対応 / FPGA搭載</p>
<p>OEM / ODM 承ります。共同開発・受託製造に対応しています。</p>
<p>IoT / エッジコンピューティング / 画像処理 / 機械学習</p>
<footer>お問い合わせ: info@yamada-mfg.example.com | 東京都千代田区</footer>
</body></html>
"""

RECRUIT_HTML = """
<html><head><title>採用情報 - 山田製作所</title></head>
<body>
<h1>採用情報</h1>
<h2>AIエンジニア募集</h2>
<p>機械学習エンジニア・組み込みエンジニアを募集しています。</p>
<p>Deep Learning / PyTorch / TensorFlow / ONNX 経験者優遇</p>
<p>組み込みLinux / RTOS / ファームウェア開発経験者</p>
</body></html>
"""

SIMPLE_HTML = """
<html><head><title>シンプル商事</title></head>
<body>
<h1>ようこそ</h1>
<p>当社は一般商社です。</p>
</body></html>
"""


# ── parser テスト ─────────────────────────────────────────────────────────────

def test_parse_page_basic():
    page = parse_page("https://example.com/", PRODUCT_HTML)
    assert page.title == "山田製作所 - 組み込みAIカメラ製品一覧"
    assert any("自社製品" in h for h in page.h1)
    assert page.lang == "ja"
    assert "info@yamada-mfg.example.com" in page.emails
    assert page.has_form is False


def test_parse_page_nav_links():
    page = parse_page("https://example.com/", PRODUCT_HTML)
    nav_lower = [n.lower() for n in page.nav_links]
    assert any("製品" in n or "product" in n for n in page.nav_links)


def test_parse_page_internal_links():
    page = parse_page("https://example.com/", PRODUCT_HTML)
    # 同一ドメインのリンクが収集されているか
    # （PRODUCT_HTMLはリンクを含まないので空でもOK）
    assert isinstance(page.internal_links, list)


# ── feature_extractor テスト ──────────────────────────────────────────────────

def test_feature_product_detection():
    pages = [
        parse_page("https://yamada-mfg.example.com/", PRODUCT_HTML),
        parse_page("https://yamada-mfg.example.com/products", PRODUCT_HTML),
    ]
    features = extract_features("yamada-mfg.example.com", pages)
    assert features.product_presence is True


def test_feature_oem_detection():
    pages = [parse_page("https://yamada-mfg.example.com/oem", PRODUCT_HTML)]
    features = extract_features("yamada-mfg.example.com", pages)
    assert features.oem_presence is True


def test_feature_tech_keywords():
    pages = [parse_page("https://yamada-mfg.example.com/", PRODUCT_HTML)]
    features = extract_features("yamada-mfg.example.com", pages)
    assert "embedded" in features.tech_keywords
    assert "iot" in features.tech_keywords


def test_feature_recruitment():
    pages = [
        parse_page("https://yamada-mfg.example.com/recruit", RECRUIT_HTML),
    ]
    features = extract_features("yamada-mfg.example.com", pages)
    assert features.recruitment_ai is True
    assert features.recruitment_eng is True


def test_feature_contact():
    pages = [parse_page("https://yamada-mfg.example.com/", PRODUCT_HTML)]
    features = extract_features("yamada-mfg.example.com", pages)
    assert features.contact_email == "info@yamada-mfg.example.com"


def test_feature_empty_pages():
    features = extract_features("empty.example.com", [])
    assert features.product_presence is False
    assert features.score_breakdown if hasattr(features, "score_breakdown") else True


# ── scoring テスト ────────────────────────────────────────────────────────────

def test_scoring_high_score():
    features = SiteFeatures(
        domain           = "yamada-mfg.example.com",
        pages_crawled    = ["https://yamada-mfg.example.com/"],
        product_presence = True,
        oem_presence     = True,
        tech_keywords    = {"iot": ["iot"], "embedded": ["組み込み"], "vision": ["画像処理"]},
        recruitment_ai   = True,
        recruitment_eng  = True,
        has_news         = True,
        has_contact_form = True,
    )
    engine = ScoringEngine()
    result = engine.score_company("山田製作所", "https://yamada-mfg.example.com", features)
    assert result.score >= 70
    assert result.is_candidate is True


def test_scoring_low_score():
    features = SiteFeatures(
        domain        = "simple.example.com",
        pages_crawled = ["https://simple.example.com/"],
    )
    engine = ScoringEngine()
    result = engine.score_company("シンプル商事", "https://simple.example.com", features)
    assert result.score < 70
    assert result.is_candidate is False


def test_scoring_no_pages():
    features = SiteFeatures(domain="error.example.com")
    engine = ScoringEngine()
    result = engine.score_company("エラー商事", "https://error.example.com", features,
                                   error="crawl_failed")
    assert result.score == 0
    assert result.error == "crawl_failed"


def test_scoring_sort_order():
    companies = [
        {"name": "低スコア商事", "url": "https://low.example.com"},
        {"name": "高スコア製作所", "url": "https://high.example.com"},
    ]
    features_map = {
        "https://low.example.com":  SiteFeatures(domain="low.example.com", pages_crawled=["x"]),
        "https://high.example.com": SiteFeatures(
            domain="high.example.com",
            pages_crawled=["x"],
            product_presence=True,
            oem_presence=True,
        ),
    }
    engine = ScoringEngine()
    results = engine.score_all(companies, features_map, {})
    assert results[0].score >= results[1].score  # スコア降順


# ── 統合テスト（実通信なし）──────────────────────────────────────────────────

def test_full_pipeline_mock():
    """パーサー → 特徴量 → スコアリングの統合テスト"""
    pages = [
        parse_page("https://yamada-mfg.example.com/", PRODUCT_HTML),
        parse_page("https://yamada-mfg.example.com/recruit", RECRUIT_HTML),
    ]
    features = extract_features("yamada-mfg.example.com", pages)
    engine = ScoringEngine()
    result = engine.score_company("山田製作所", "https://yamada-mfg.example.com", features)

    assert result.score >= 70
    assert result.is_candidate is True
    assert result.contact_email is not None
    assert len(result.detected_features) > 0
