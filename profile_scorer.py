"""
profile_scorer.py - プロファイル駆動スコアリングエンジン
=========================================================
YAMLで定義した評価軸・配点に基づいて企業サイトをスコアリングする。
既存の scoring_engine.py を置き換える案件プロファイル対応版。

各評価軸の detection タイプ:
  keyword_any              : いずれかのキーワードが1つでもヒットしたら加点
  keyword_count            : min_hits 以上のキーワードがヒットしたら加点
  keyword_and_pattern      : キーワード OR 正規表現パターンでヒット
  keyword_groups_all       : keyword_groups の各グループで1件以上ヒットしたら加点
  keyword_any_on_page      : 特定URLパスのページでキーワードがヒット
  recent_keyword_any_on_page: 特定ページでKWと直近N年の日付が同時に確認できたら加点
  url_exists               : url_signalsいずれかのURLが存在したら加点
  contact                  : メールアドレスまたはお問い合わせフォームが存在
  regex                    : 正規表現マッチ
  manual                   : 自動判定せず要確認として出力
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class AxisResult:
    """1評価軸／調査項目の判定結果"""
    id:           str
    name:         str
    points:       int
    earned:       bool        # 加点またはEvidence検出されたか
    score:        int         # 採点軸ではpoints、調査項目では通常0
    hit_keywords: list[str] = field(default_factory=list)
    detail:       str = ""


@dataclass
class ProfileScore:
    """プロファイルスコアリングの結果"""
    company_name:  str
    company_url:   str
    profile_name:  str
    total_score:   int
    raw_score:     int         # キャップ前の合計
    score_cap:     int
    judgment:      str         # ◎/○/△/－ または S/A/B/C 等
    axes:          list[AxisResult] = field(default_factory=list)
    report_fields: list[AxisResult] = field(default_factory=list)

    @property
    def is_excellent(self) -> bool:
        return self.judgment in ("◎", "S")

    @property
    def is_candidate(self) -> bool:
        return self.judgment in ("◎", "○", "S", "A")

    def summary(self) -> str:
        lines = [
            f"[{self.profile_name}] {self.company_name}",
            f"  スコア: {self.total_score}点 ({self.judgment})",
        ]
        for ax in self.axes:
            mark = "✓" if ax.earned else "✗"
            lines.append(f"  {mark} {ax.name}: {ax.score}点"
                         + (f" ({', '.join(ax.hit_keywords[:3])})" if ax.hit_keywords else ""))
        return "\n".join(lines)


# ════════════════════════════════════════════════════════
#  テキスト解析ヘルパー
# ════════════════════════════════════════════════════════

def _normalize(text: str) -> str:
    return text.lower().replace("\u3000", " ").replace("\n", " ")


def _hit_keywords(text_lower: str, keywords: list[str]) -> list[str]:
    return [kw for kw in keywords if kw.lower() in text_lower]


def _hit_patterns(text: str, patterns: list[str]) -> bool:
    for pat in patterns:
        try:
            if re.search(pat, text):
                return True
        except re.error:
            pass
    return False


def _pages_with_path(pages: list, path_signals: list[str]) -> list:
    """URLパスがシグナルに一致するページを返す"""
    result = []
    for p in pages:
        url_lower = getattr(p, "url", "").lower()
        for sig in path_signals:
            if sig.lower() in url_lower:
                result.append(p)
                break
    return result


def _all_text_from_pages(pages: list) -> str:
    """全ページのテキストを結合"""
    parts = []
    for p in pages:
        for attr in ["title", "body_text", "meta_desc", "footer_text"]:
            val = getattr(p, attr, "")
            if val:
                parts.append(val if isinstance(val, str) else " ".join(val))
        for attr in ["h1", "h2", "h3"]:
            val = getattr(p, attr, [])
            if isinstance(val, list):
                parts.extend(val)
    return " ".join(parts)


def _has_contact(pages: list) -> bool:
    """メールアドレスまたはフォームの存在確認"""
    all_text = _all_text_from_pages(pages).lower()
    if re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", all_text):
        return True
    contact_words = ["お問い合わせ", "contact", "問い合わせフォーム", "inquiry"]
    return any(w.lower() in all_text for w in contact_words)


def _url_exists(pages: list, url_signals: list[str]) -> bool:
    """いずれかのURLシグナルに一致するページが存在するか"""
    for p in pages:
        url_lower = getattr(p, "url", "").lower()
        for sig in url_signals:
            if sig.lower() in url_lower:
                return True
    return False


def _contains_recent_date(text: str, recent_years: int) -> tuple[bool, str]:
    """本文中の日付が概ね直近N年かを判定する。

    完全な日付は今日からN*365日前で比較する。年だけしか取れない場合は
    現在年-N年以上を補助Evidenceとして扱う。
    """
    today = date.today()
    cutoff = today - timedelta(days=max(1, recent_years) * 365)

    full_date_patterns = [
        r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})",
        r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",
    ]
    for pat in full_date_patterns:
        for match in re.finditer(pat, text):
            try:
                d = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
            if cutoff <= d <= today + timedelta(days=31):
                return True, d.isoformat()

    years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)]
    recent_year_floor = today.year - max(1, recent_years)
    recent = [y for y in years if recent_year_floor <= y <= today.year]
    if recent:
        return True, str(max(recent))
    return False, ""


# ════════════════════════════════════════════════════════
#  軸ごとの判定
# ════════════════════════════════════════════════════════

def _evaluate_axis(axis: dict[str, Any], pages: list, all_text: str) -> AxisResult:
    """1評価軸または調査項目を判定してAxisResultを返す"""
    ax_id      = axis.get("id", "")
    ax_name    = axis.get("name", ax_id)
    ax_points  = int(axis.get("points", 0))
    detection  = axis.get("detection", "keyword_any")
    keywords   = axis.get("keywords", [])
    patterns   = axis.get("patterns", [])
    url_sigs   = axis.get("url_signals", [])
    min_hits   = axis.get("min_hits", 1)
    tgt_pages  = axis.get("target_pages", [])

    text_lower = _normalize(all_text)
    earned     = False
    hits: list[str] = []
    detail     = ""

    if detection == "keyword_any":
        hits = _hit_keywords(text_lower, keywords)
        earned = len(hits) > 0

    elif detection == "keyword_count":
        hits = _hit_keywords(text_lower, keywords)
        earned = len(hits) >= min_hits

    elif detection == "keyword_and_pattern":
        hits = _hit_keywords(text_lower, keywords)
        pat_hit = _hit_patterns(all_text, patterns)
        url_hit = _url_exists(pages, url_sigs) if url_sigs else False
        earned = len(hits) > 0 or pat_hit or url_hit
        details = []
        if pat_hit:
            details.append("型番/スペックパターン検出")
        if url_hit:
            details.append("対象URL検出")
        detail = " / ".join(details)

    elif detection == "keyword_groups_all":
        groups = axis.get("keyword_groups", []) or []
        group_hits: list[list[str]] = []
        for group in groups:
            gh = _hit_keywords(text_lower, group)
            group_hits.append(gh)
            hits.extend(gh)
        earned = bool(groups) and all(group_hits)
        detail = " / ".join(
            f"G{i + 1}:{len(gh)}件" for i, gh in enumerate(group_hits)
        )

    elif detection == "keyword_any_on_page":
        target = _pages_with_path(pages, tgt_pages) if tgt_pages else pages
        if target:
            page_text = _normalize(_all_text_from_pages(target))
            hits = _hit_keywords(page_text, keywords)
            earned = len(hits) > 0
            detail = f"対象ページ{len(target)}件"
        else:
            detail = "対象ページなし"

    elif detection == "recent_keyword_any_on_page":
        target = _pages_with_path(pages, tgt_pages) if tgt_pages else pages
        if target:
            target_text = _all_text_from_pages(target)
            hits = _hit_keywords(_normalize(target_text), keywords)
            recent, recent_evidence = _contains_recent_date(
                target_text,
                int(axis.get("recent_years", 2)),
            )
            earned = bool(hits) and recent
            if recent_evidence:
                detail = f"直近日付Evidence:{recent_evidence}"
            elif hits:
                detail = "新製品KWあり／直近日付未確認"
            else:
                detail = "新製品KW未確認"
        else:
            detail = "対象ページなし"

    elif detection == "regex":
        pattern = axis.get("pattern", "")
        try:
            m = re.search(pattern, all_text) if pattern else None
            earned = m is not None
            if earned:
                detail = f"正規表現マッチ: {m.group()[:50]}"
        except re.error as e:
            detail = f"正規表現エラー: {e}"

    elif detection == "keyword_all":
        hits = _hit_keywords(text_lower, keywords)
        earned = len(hits) >= len(keywords)
        detail = f"{len(hits)}/{len(keywords)}キーワードヒット"

    elif detection == "url_exists":
        earned = _url_exists(pages, url_sigs)
        if earned:
            detail = "対象URL検出"

    elif detection == "contact":
        earned = _has_contact(pages)
        if earned:
            detail = "メール/フォーム検出"

    elif detection == "manual":
        earned = False
        detail = axis.get("unverified_text", "要確認")

    return AxisResult(
        id=ax_id,
        name=ax_name,
        points=ax_points,
        earned=earned,
        score=ax_points if earned else 0,
        hit_keywords=list(dict.fromkeys(hits))[:5],
        detail=detail,
    )


# ════════════════════════════════════════════════════════
#  ProfileScorer クラス
# ════════════════════════════════════════════════════════

class ProfileScorer:
    """YAMLプロファイルの scoring_axes に基づいてスコアリングする。"""

    def __init__(self, profile) -> None:
        self.profile    = profile
        self.axes_defs  = getattr(profile, "scoring_axes", [])
        self.report_defs = getattr(profile, "report_fields", [])
        self.score_cap  = getattr(profile, "score_cap", 100)
        self.score_sum_max = getattr(profile, "score_sum_max", 100)
        self.threshold_excellent = getattr(profile, "threshold_excellent", 70)
        self.threshold_good      = getattr(profile, "threshold_good", 50)

    def score(
        self,
        company_name: str,
        company_url:  str,
        parsed_pages: list,
    ) -> ProfileScore:
        all_text = _all_text_from_pages(parsed_pages)
        axes_results: list[AxisResult] = []

        for axis_def in self.axes_defs:
            axes_results.append(_evaluate_axis(axis_def, parsed_pages, all_text))

        report_results: list[AxisResult] = []
        for report_def in self.report_defs:
            report_def = dict(report_def)
            report_def["points"] = 0
            report_results.append(_evaluate_axis(report_def, parsed_pages, all_text))

        raw_score = sum(ax.score for ax in axes_results)

        bonus_total = 0
        bonus_rules = getattr(self.profile, "bonus_rules", []) or []
        for rule in bonus_rules:
            conditions = rule.get("condition", [])
            bonus      = rule.get("bonus", 0)
            if all(kw.lower() in _normalize(all_text) for kw in conditions):
                bonus_total += bonus
                log.debug(f"  ボーナス適用: {rule.get('name','')} +{bonus}点")

        raw_score  += bonus_total
        total_score = min(raw_score, self.score_cap)
        if raw_score <= -100:
            total_score = raw_score

        if hasattr(self.profile, "judge"):
            judgment = self.profile.judge(total_score)
        elif total_score >= self.threshold_excellent:
            judgment = "◎"
        elif total_score >= self.threshold_good:
            judgment = "○"
        elif total_score > 0:
            judgment = "△"
        else:
            judgment = "－"

        return ProfileScore(
            company_name=company_name,
            company_url=company_url,
            profile_name=self.profile.name,
            total_score=total_score,
            raw_score=raw_score,
            score_cap=self.score_cap,
            judgment=judgment,
            axes=axes_results,
            report_fields=report_results,
        )


def create_scorer_from_profile(profile) -> ProfileScorer:
    """Profile オブジェクトから ProfileScorer を生成"""
    return ProfileScorer(profile)
