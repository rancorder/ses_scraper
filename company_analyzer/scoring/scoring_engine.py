"""
scoring/scoring_engine.py - スコアリングエンジン
  SiteFeatures → ScoredCompany
  将来のAI分類追加を想定したプラガブル設計
"""
from __future__ import annotations

import logging
from typing import Protocol

from company_analyzer.config import SCORE_CFG
from company_analyzer.models import SiteFeatures, ScoredCompany

log = logging.getLogger(__name__)


# ── スコアリング戦略インターフェース（将来のAI分類対応） ────────────────
class ScoringStrategy(Protocol):
    def score(self, features: SiteFeatures) -> tuple[int, dict[str, int]]:
        """(合計スコア, 内訳dict) を返す"""
        ...


class RuleBasedScoring:
    """ルールベーススコアリング（デフォルト）"""

    def score(self, features: SiteFeatures) -> tuple[int, dict[str, int]]:
        w = SCORE_CFG.weights
        breakdown: dict[str, int] = {}

        # 製品保有（最重要）
        breakdown["product_presence"] = w["product_presence"] if features.product_presence else 0

        # OEM / 共同開発文化
        breakdown["oem_presence"] = w["oem_presence"] if features.oem_presence else 0

        # 技術キーワード（カテゴリごとに判定）
        kw = features.tech_keywords
        breakdown["iot_keyword"]      = w["iot_keyword"]      if "iot"      in kw else 0
        breakdown["embedded_keyword"] = w["embedded_keyword"] if "embedded" in kw else 0
        breakdown["vision_keyword"]   = w["vision_keyword"]   if "vision"   in kw else 0

        # 採用シグナル（企業の技術戦略を反映）
        breakdown["recruitment_ai"]  = w["recruitment_ai"]  if features.recruitment_ai  else 0
        breakdown["recruitment_eng"] = w["recruitment_eng"] if features.recruitment_eng else 0

        # サイト活動（企業の活発度）
        has_activity = features.has_news or features.has_blog or features.has_press
        breakdown["site_activity"] = w["site_activity"] if has_activity else 0

        # コンタクト品質（リーチしやすさ）
        has_contact = bool(features.contact_email) or features.has_contact_form
        breakdown["contact_quality"] = w["contact_quality"] if has_contact else 0

        total = sum(breakdown.values())
        return total, breakdown


class ScoringEngine:
    """
    スコアリングエンジン本体。
    strategy を差し替えることでAI分類に移行可能。
    """

    def __init__(self, strategy: ScoringStrategy | None = None):
        self.strategy = strategy or RuleBasedScoring()

    def score_company(
        self,
        company_name: str,
        company_url: str,
        features: SiteFeatures,
        error: str | None = None,
    ) -> ScoredCompany:
        """1社のスコアリングを実行"""

        if error or not features.pages_crawled:
            return ScoredCompany(
                company_name     = company_name,
                company_url      = company_url,
                domain           = features.domain,
                score            = 0,
                is_candidate     = False,
                error            = error or "no_pages_crawled",
            )

        total, breakdown = self.strategy.score(features)

        # 検出特徴量の人間可読リスト
        detected: list[str] = []
        if features.product_presence:
            detected.append("自社製品あり")
        if features.oem_presence:
            detected.append("OEM/共同開発対応")
        for cat, kws in features.tech_keywords.items():
            detected.append(f"{cat}: {', '.join(kws[:3])}")
        if features.recruitment_ai:
            detected.append("AI系エンジニア募集中")
        if features.recruitment_eng:
            detected.append("組み込み系エンジニア募集中")
        if features.has_news or features.has_blog:
            detected.append("サイト更新あり")

        return ScoredCompany(
            company_name      = company_name,
            company_url       = company_url,
            domain            = features.domain,
            score             = min(total, 100),   # 100点キャップ
            is_candidate      = total >= SCORE_CFG.threshold,
            score_breakdown   = breakdown,
            detected_features = detected,
            product_pages     = features.product_pages,
            oem_pages         = features.oem_pages,
            tech_keywords     = features.tech_keywords,
            contact_email     = features.contact_email,
            has_contact_form  = features.has_contact_form,
            pages_crawled     = len(features.pages_crawled),
        )

    def score_all(
        self,
        companies: list[dict],
        features_map: dict[str, SiteFeatures],
        errors_map: dict[str, str],
    ) -> list[ScoredCompany]:
        """全企業をスコアリングしてスコア降順で返す"""
        results = []
        for company in companies:
            url  = company.get("url", "")
            name = company.get("name", url)
            feat = features_map.get(url) or SiteFeatures(domain=url)
            err  = errors_map.get(url)
            scored = self.score_company(name, url, feat, err)
            results.append(scored)
            log.debug(f"  {name}: {scored.score}点 (候補: {scored.is_candidate})")

        results.sort(key=lambda c: c.score, reverse=True)
        return results