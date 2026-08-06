from __future__ import annotations

import re
from pathlib import Path

import yaml

PROFILE_PATH = Path(__file__).resolve().parents[1] / "config" / "profiles" / "t2_lab.yaml"
PRIMARY_AXES = ["manufacturer_detection", "own_hw_product", "circuit_design"]


def evaluate_axis(axis: dict, text: str) -> bool:
    detection = axis.get("detection", "keyword_any")
    lowered = text.lower()

    if detection == "keyword_any":
        return any(str(k).lower() in lowered for k in axis.get("keywords", []))
    if detection == "keyword_count":
        hits = {str(k).lower() for k in axis.get("keywords", []) if str(k).lower() in lowered}
        return len(hits) >= int(axis.get("min_hits", 1))
    if detection == "keyword_all":
        keywords = [str(k).lower() for k in axis.get("keywords", [])]
        return all(k in lowered for k in keywords)
    if detection == "regex":
        return re.search(axis.get("pattern", ""), text) is not None

    raise ValueError(f"unsupported detection: {detection}")


def score(profile: dict, text: str) -> tuple[dict[str, bool], int]:
    results: dict[str, bool] = {}
    total = 0

    for axis in profile["scoring_axes"]:
        earned = evaluate_axis(axis, text)
        results[axis["id"]] = earned
        if earned:
            total += int(axis.get("points", 0))

    return results, min(total, int(profile.get("score_cap", 100)))


def marks(results: dict[str, bool]) -> str:
    return "".join("○" if results[axis_id] else "×" for axis_id in PRIMARY_AXES)


def test_t2_primary_axes_against_representative_cases() -> None:
    profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    assert profile["primary_axis_ids"] == PRIMARY_AXES

    cases = [
        (
            "電気制御機器メーカー",
            "自社工場で制御装置を製造・量産。自社ブランドの検査装置を提供。制御基板と電子回路を自社設計開発。エッジAIにも対応。",
            "○○○",
        ),
        (
            "素材系自社製品メーカー",
            "自社工場で樹脂材料を製造・加工。自社製品として独自製品の樹脂デバイス材料を販売。電子回路の設計は行わない。",
            "○○×",
        ),
        (
            "受託金属加工専業",
            "金属加工と受託加工を行い、量産にも対応。顧客図面に基づく部品製造で、自社ブランド製品は保有しない。",
            "○××",
        ),
        (
            "電子回路設計専業",
            "自社工場や自社製品は持たず、電子回路と制御基板の受託設計、ハードウェア開発を請け負う。",
            "××○",
        ),
        (
            "FPGA販売代理店",
            "海外メーカーのFPGA、制御基板、マイコンを正規代理店として販売。製品一覧と型番を掲載。自社設計や製造は行わない。",
            "×××",
        ),
        (
            "製造業向けDX会社",
            "製造業向け、工場向けの生産管理SaaSとDXソリューションを提供。ソフトウェア開発専業。",
            "×××",
        ),
        (
            "基板実装のみ",
            "プリント基板の部品実装と組立を受託。回路設計は顧客支給で、設計業務は行わない。受託製造に対応。",
            "○××",
        ),
        (
            "自社SaaSのみ",
            "自社製品としてクラウドサービスを提供するSaaS専業企業。ハードウェアは扱わない。",
            "×××",
        ),
    ]

    failures = []
    for name, text, expected in cases:
        results, total = score(profile, text)
        actual = marks(results)
        if actual != expected:
            failures.append({"name": name, "expected": expected, "actual": actual, "score": total})

    assert not failures, failures
