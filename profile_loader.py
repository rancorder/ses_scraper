"""
profile_loader.py - 案件プロファイルローダー（scoring_axes対応版）
================================================================
YAMLプロファイルを読み込み、ses_pipeline / profile_scorer が使える形に変換する。

使い方:
    from profile_loader import load_profile, list_profiles, show_profiles

    profile = load_profile("design")
    print(profile.name)
    print(profile.scoring_axes)       # 評価軸リスト
    print(profile.all_client_keywords) # キーワード一覧
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).resolve().parent / "config" / "profiles"


@dataclass
class Profile:
    """案件プロファイル（1案件の設定一式）"""
    slug:               str
    name:               str
    description:        str
    keywords_search:    list[str]
    # 評価軸（YAMLのscoring_axes）
    scoring_axes:       list[dict[str, Any]] = field(default_factory=list)
    # 採点外だが結果Excelへ出す調査項目
    report_fields:      list[dict[str, Any]] = field(default_factory=list)
    # クロール対象パス（プロファイルごとに上書き可能）
    crawl_paths:        list[str] | None = None
    # キーワード辞書（ses_pipeline の _keyword_scan 用）
    client_keywords:    dict[str, list[str]] = field(default_factory=dict)
    partner_keywords:   dict[str, list[str]] = field(default_factory=dict)
    # 閾値（従来の ◎ / ○ 判定）
    client_threshold:   int = 50
    partner_threshold:  int = 50
    threshold_excellent: int = 70
    threshold_good:      int = 50
    # S/A/B/C 等の案件固有ランク。未指定なら従来判定を使用。
    rank_thresholds:    dict[str, int] = field(default_factory=dict)
    candidate_ranks:    list[str] = field(default_factory=list)
    score_cap:           int = 100
    score_sum_max:       int = 100
    # プロファイルスコアへ加えるAI補正率。既存互換の既定値は15%。
    ai_score_bonus_weight: float = 0.15
    # ボーナスルール（複合シグナル）
    bonus_rules:        list[dict] = field(default_factory=list)
    # Ollama指示
    ai_instruction:     str = ""

    # ── 互換ヘルパー ────────────────────────────────────────────────

    @property
    def all_client_keywords(self) -> list[str]:
        """client_keywords の全キーワードをフラットリストで返す"""
        result = []
        for words in self.client_keywords.values():
            result.extend(words)
        # scoring_axesからも収集（target_keywordsがない場合のフォールバック）
        if not result:
            for ax in self.scoring_axes:
                result.extend(ax.get("keywords", []))
                for group in ax.get("keyword_groups", []):
                    result.extend(group)
        return result

    @property
    def all_partner_keywords(self) -> list[str]:
        result = []
        for words in self.partner_keywords.values():
            result.extend(words)
        return result

    def judge(self, score: int) -> str:
        """案件固有ランク、または従来の ◎/○/△/－ を返す。"""
        if self.rank_thresholds:
            ordered = sorted(
                self.rank_thresholds.items(),
                key=lambda item: item[1],
                reverse=True,
            )
            for label, minimum in ordered:
                if score >= int(minimum):
                    return str(label)
            # 最低閾値を下回る場合も最下位ランクへ寄せる。
            return str(ordered[-1][0]) if ordered else "－"

        if score >= self.threshold_excellent:
            return "◎"
        if score >= self.threshold_good:
            return "○"
        if score > 0:
            return "△"
        return "－"

    @property
    def effective_candidate_ranks(self) -> list[str]:
        if self.candidate_ranks:
            return self.candidate_ranks
        if self.rank_thresholds:
            # 案件側で未指定なら上位2ランクを候補扱いにする。
            ordered = sorted(
                self.rank_thresholds.items(),
                key=lambda item: item[1],
                reverse=True,
            )
            return [str(label) for label, _ in ordered[:2]]
        return ["◎", "○"]

    def scoring_prompt(self) -> str:
        """Ollama用プロンプト生成"""
        return f"""
あなたはBtoB営業支援AIです。
企業サイトのテキストを分析し、営業案件「{self.name}」の観点でスコアリングしてください。

{self.ai_instruction}

以下のJSON形式のみで回答してください:
{{
  "client_score": 0-100の整数,
  "partner_score": 0-100の整数,
  "reason": "判定理由を1〜2文で",
  "sales_talk": "この企業への具体的な営業トーク1〜2文"
}}
""".strip()


def load_profile(slug: str) -> Profile:
    """
    プロファイルYAMLを読み込む。

    Parameters
    ----------
    slug : "ses" / "design" / "edge_ai" またはYAMLファイルのフルパス
    """
    try:
        import yaml
    except ImportError:
        raise ImportError("PyYAML が必要です: pip install pyyaml")

    path = Path(slug)
    if not path.exists():
        path = PROFILES_DIR / f"{slug}.yaml"
    if not path.exists():
        available = list_profiles()
        raise FileNotFoundError(
            f"プロファイル '{slug}' が見つかりません。\n"
            f"利用可能: {available}\n"
            f"検索パス: {PROFILES_DIR}"
        )

    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)

    thresholds = data.get("thresholds", {})

    # target_keywords（後方互換）
    kws = data.get("target_keywords", {})
    client_kws  = kws.get("client",  {})
    partner_kws = kws.get("partner", {})

    # scoring_axesからもclient_keywordsを補完
    if not client_kws:
        axes = data.get("scoring_axes", [])
        for ax in axes:
            kw_list = list(ax.get("keywords", []))
            for group in ax.get("keyword_groups", []):
                kw_list.extend(group)
            if kw_list:
                client_kws[ax.get("id", ax.get("name", ""))] = list(dict.fromkeys(kw_list))

    rank_thresholds = {
        str(k): int(v)
        for k, v in (data.get("rank_thresholds", {}) or {}).items()
    }

    profile = Profile(
        slug              = path.stem,
        name              = data.get("name", path.stem),
        description       = data.get("description", ""),
        keywords_search   = data.get("keywords_search", []),
        scoring_axes      = data.get("scoring_axes", []),
        report_fields     = data.get("report_fields", []),
        crawl_paths       = data.get("crawl_paths", None),
        client_keywords   = client_kws,
        partner_keywords  = partner_kws,
        client_threshold  = thresholds.get("client", 50),
        partner_threshold = thresholds.get("partner", 50),
        threshold_excellent = thresholds.get("excellent", 70),
        threshold_good      = thresholds.get("good", 50),
        rank_thresholds   = rank_thresholds,
        candidate_ranks   = [str(v) for v in data.get("candidate_ranks", [])],
        score_cap         = data.get("score_cap", 100),
        score_sum_max     = data.get("score_sum_max", 100),
        ai_score_bonus_weight = float(data.get("ai_score_bonus_weight", 0.15)),
        bonus_rules       = data.get("bonus_rules", []),
        ai_instruction    = data.get("ai_instruction", ""),
    )

    if profile.rank_thresholds:
        rank_text = " / ".join(
            f"{label}:{minimum}+"
            for label, minimum in sorted(
                profile.rank_thresholds.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        )
    else:
        rank_text = f"◎{profile.threshold_excellent}点 ○{profile.threshold_good}点"

    log.info(
        f"[Profile] '{profile.name}' 読み込み完了 | "
        f"検索KW:{len(profile.keywords_search)} | "
        f"評価軸:{len(profile.scoring_axes)}軸 | "
        f"調査項目:{len(profile.report_fields)} | "
        f"判定:{rank_text}"
    )
    return profile


def list_profiles() -> list[str]:
    if not PROFILES_DIR.exists():
        return []
    return [p.stem for p in sorted(PROFILES_DIR.glob("*.yaml"))]


def show_profiles() -> None:
    """利用可能なプロファイルを表示"""
    try:
        import yaml
    except ImportError:
        print("pip install pyyaml が必要です")
        return

    profiles = list_profiles()
    if not profiles:
        print(f"プロファイルが見つかりません: {PROFILES_DIR}")
        return

    print("\n" + "=" * 60)
    print("  利用可能な案件プロファイル")
    print("=" * 60)
    for slug in profiles:
        try:
            p = load_profile(slug)
            print(f"\n  [{slug}]  {p.name}")
            print(f"    説明   : {p.description[:50]}")
            print(f"    検索KW : {', '.join(p.keywords_search[:4])}{'...' if len(p.keywords_search)>4 else ''}")
            print(f"    評価軸 : {len(p.scoring_axes)}軸  合計{sum(ax.get('points',0) for ax in p.scoring_axes)}点")
            if p.rank_thresholds:
                print("    判定   : " + " / ".join(
                    f"{label}{minimum}点以上"
                    for label, minimum in sorted(
                        p.rank_thresholds.items(),
                        key=lambda item: item[1],
                        reverse=True,
                    )
                ))
            else:
                print(f"    判定   : ◎{p.threshold_excellent}点以上  ○{p.threshold_good}点以上")
        except Exception as e:
            print(f"  [{slug}] 読み込みエラー: {e}")
    print("=" * 60 + "\n")
