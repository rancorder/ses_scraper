"""
pipeline/pipeline.py - 全モジュールを繋ぐパイプライン
  crawler → parser → feature_extractor → scoring → storage
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

from company_analyzer.crawler.crawler import crawl_all
from company_analyzer.feature_extractor.extractor import extract_features
from company_analyzer.models import SiteFeatures, ScoredCompany
from company_analyzer.parser.site_parser import parse_page
from company_analyzer.scoring.scoring_engine import ScoringEngine
from company_analyzer.storage.storage import save_csv, save_excel, save_json

log = logging.getLogger(__name__)


def _extract_domain(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or url


async def run_pipeline(
    companies: list[dict],
    output_prefix: str = "analysis",
    concurrency: int | None = None,
    save_formats: list[str] | None = None,
) -> list[ScoredCompany]:
    """
    メインパイプライン。
    入力: [{"name": ..., "url": ...}, ...]
    出力: スコア順のScoredCompanyリスト
    """
    save_formats = save_formats or ["excel", "csv", "json"]
    total = len(companies)
    log.info(f"=" * 60)
    log.info(f"解析開始: {total} 社")
    log.info(f"=" * 60)

    # ── Step 1: クロール ──────────────────────────────────────
    log.info("\n[1/4] クロール中...")
    crawl_results = await crawl_all(companies, concurrency)
    crawled_count = sum(1 for v in crawl_results.values() if v)
    log.info(f"  クロール完了: {crawled_count}/{total} サイト")

    # ── Step 2: パース ────────────────────────────────────────
    log.info("\n[2/4] HTML解析中...")
    parsed_map: dict[str, list] = {}
    for i, (url, pages) in enumerate(crawl_results.items(), 1):
        parsed_map[url] = [parse_page(p.url, p.html) for p in pages if p.html]
        if i % 200 == 0 or i == total:
            log.info(f"  パース進捗: {i}/{total}")
    total_pages = sum(len(v) for v in parsed_map.values())
    log.info(f"  パース完了: {total_pages} ページ")

    # ── Step 3: 特徴量抽出 ────────────────────────────────────
    log.info("\n[3/4] 特徴量抽出中...")
    features_map: dict[str, SiteFeatures] = {}
    errors_map:   dict[str, str]          = {}

    for i, (url, parsed_pages) in enumerate(parsed_map.items(), 1):
        domain = _extract_domain(url)
        if not parsed_pages:
            errors_map[url] = "no_pages_fetched"
            features_map[url] = SiteFeatures(domain=domain)
        else:
            features_map[url] = extract_features(domain, parsed_pages)
        if i % 200 == 0 or i == total:
            log.info(f"  特徴量抽出進捗: {i}/{total}")

    # クロール自体のエラー（接続失敗など）
    for url, pages in crawl_results.items():
        if not pages and url not in errors_map:
            errors_map[url] = "crawl_failed"

    log.info(f"  特徴量抽出完了")

    # ── Step 4: スコアリング ──────────────────────────────────
    log.info("\n[4/4] スコアリング中...")
    engine = ScoringEngine()
    scored = engine.score_all(companies, features_map, errors_map)
    candidates = [s for s in scored if s.is_candidate]
    log.info(f"  スコアリング完了: {len(candidates)}/{total} 社が営業候補（70点以上）")

    # ── 出力 ─────────────────────────────────────────────────
    log.info("\n結果を保存中...")
    from company_analyzer.config import OUTPUT_DIR
    prefix = OUTPUT_DIR / output_prefix

    if "excel" in save_formats:
        save_excel(scored, Path(str(prefix) + ".xlsx"))
    if "csv" in save_formats:
        save_csv(scored, Path(str(prefix) + ".csv"))
    if "json" in save_formats:
        save_json(scored, Path(str(prefix) + ".json"))

    # サマリーをログに出力
    log.info("\n" + "=" * 60)
    log.info("  🏆  TOP 10 営業候補企業")
    log.info("=" * 60)
    for i, c in enumerate(candidates[:10], 1):
        kw_cats = list(c.tech_keywords.keys())
        log.info(
            f"  {i:2}. [{c.score:3d}点] {c.company_name}"
            f"  技術: {','.join(kw_cats[:3])}"
        )

    return scored
