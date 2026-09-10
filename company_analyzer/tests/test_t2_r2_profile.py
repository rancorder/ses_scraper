from datetime import date

from company_analyzer.parser.site_parser import ParsedPage
from profile_loader import load_profile
from profile_scorer import ProfileScorer
from ses_pipeline import SesResult


def _page(url: str, text: str) -> ParsedPage:
    return ParsedPage(url=url, title="test", body_text=text)


def _axis(result, axis_id: str):
    return next(a for a in result.axes if a.id == axis_id)


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
            "画像処理 研究開発部 新製品 発売 " + today,
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
            "自社製品 電子機器 電子回路設計 組込みLinux 画像処理 "
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


def test_bare_ai_word_does_not_earn_ai_vision_points():
    profile = load_profile("t2_lab")
    pages = [_page("https://example.com/", "生成AI時代に向けた経営方針を公開しました")]
    result = ProfileScorer(profile).score("Example", "https://example.com", pages)
    assert _axis(result, "ai_vision").score == 0


def test_development_and_recruiting_real_world_phrasing_is_detected():
    profile = load_profile("t2_lab")
    pages = [
        _page(
            "https://example.com/company/profile",
            "開発拠点としてテクニカルセンターを設置し、研究開発を行っています。",
        ),
        _page(
            "https://example.com/recruit/",
            "新卒採用 総合職 設計開発 コネクタの設計開発 電気電子系",
        ),
    ]
    result = ProfileScorer(profile).score("Example", "https://example.com", pages)
    assert _axis(result, "development_department").score == 10
    assert _axis(result, "engineer_recruiting").score == 5


def test_recent_product_release_can_be_detected_on_homepage():
    profile = load_profile("t2_lab")
    pages = [
        _page(
            "https://example.com/",
            "2025.05.20 新製品情報 新型コネクタを発売しました。",
        )
    ]
    result = ProfileScorer(profile).score("Example", "https://example.com", pages)
    assert _axis(result, "active_new_product").score == 10
