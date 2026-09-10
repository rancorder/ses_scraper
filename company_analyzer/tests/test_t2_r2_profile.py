from datetime import date

from company_analyzer.parser.site_parser import ParsedPage
from profile_loader import load_profile
from profile_scorer import ProfileScorer
from ses_pipeline import SesResult


def _page(url: str, text: str) -> ParsedPage:
    return ParsedPage(url=url, title="test", body_text=text)


def test_t2_r2_profile_definition_is_100_points():
    profile = load_profile("t2_lab")
    assert len(profile.scoring_axes) == 8
    assert sum(axis["points"] for axis in profile.scoring_axes) == 100
    assert profile.rank_thresholds == {"S": 90, "A": 80, "B": 70, "C": 0}
    assert profile.effective_candidate_ranks == ["S", "A"]
    assert profile.ai_score_bonus_weight == 0.0
    assert len(profile.report_fields) == 13


def test_t2_r2_full_match_scores_s_rank():
    profile = load_profile("t2_lab")
    today = date.today().isoformat()
    pages = [
        _page(
            "https://example.com/products",
            "自社製品 電子機器 電子回路設計 FPGA開発 組込みLinux "
            "AI 画像処理 研究開発部 新製品 発売 " + today,
        ),
        _page(
            "https://example.com/recruit",
            "技術者募集 回路設計 FPGA 組込み 開発職",
        ),
    ]
    result = ProfileScorer(profile).score("Example", "https://example.com", pages)
    assert result.total_score == 100
    assert result.judgment == "S"


def test_t2_r2_rank_boundaries():
    profile = load_profile("t2_lab")
    assert profile.judge(100) == "S"
    assert profile.judge(90) == "S"
    assert profile.judge(89) == "A"
    assert profile.judge(80) == "A"
    assert profile.judge(79) == "B"
    assert profile.judge(70) == "B"
    assert profile.judge(69) == "C"
    assert profile.judge(0) == "C"


def test_t2_r2_ai_reference_score_does_not_change_formal_score():
    profile = load_profile("t2_lab")
    today = date.today().isoformat()
    pages = [
        _page(
            "https://example.com/products",
            "自社製品 電子機器 電子回路設計 組込みLinux AI 画像処理 "
            "研究開発部 新製品 発売 " + today,
        )
    ]
    profile_score = ProfileScorer(profile).score(
        "Example", "https://example.com", pages
    )
    assert profile_score.total_score == 80
    assert profile_score.judgment == "A"

    result = SesResult(
        company_name="Example",
        company_url="https://example.com",
        profile_score=profile_score,
        ai_client_score=100,
    )
    result.calc_final_scores(profile=profile)
    assert result.final_client_score == 80
    assert result.judgment == "A"
