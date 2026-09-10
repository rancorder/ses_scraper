from datetime import date, timedelta

from company_analyzer.parser.site_parser import ParsedPage
from profile_loader import load_profile
from profile_scorer import ProfileScorer
from ses_pipeline import SesResult


def _page(url: str, text: str, title: str = "test") -> ParsedPage:
    return ParsedPage(url=url, title=title, body_text=text)


def _report(result, field_id: str):
    return next(item for item in result.report_fields if item.id == field_id)


def test_t2_r2_profile_definition_is_100_points():
    profile = load_profile("t2_lab")
    assert len(profile.scoring_axes) == 8
    assert sum(axis["points"] for axis in profile.scoring_axes) == 100
    assert profile.rank_thresholds == {"S": 90, "A": 80, "B": 70, "C": 0}
    assert profile.effective_candidate_ranks == ["S", "A"]
    assert profile.ai_score_bonus_weight == 0.0
    assert len(profile.report_fields) == 20


def test_t2_r2_full_match_scores_s_rank():
    profile = load_profile("t2_lab")
    today = date.today().isoformat()
    pages = [
        _page(
            "https://example.com/products",
            "自社製品 電子機器 電子回路設計 FPGA開発 組込みLinux "
            "AI技術 画像処理 研究開発部 新製品 発売 " + today,
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
            "自社製品 電子機器 電子回路設計 組込みLinux AI技術 画像処理 "
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


def test_exhibition_upcoming_and_recent_are_report_only():
    profile = load_profile("t2_lab")
    future = (date.today() + timedelta(days=90)).isoformat()
    past = (date.today() - timedelta(days=120)).isoformat()
    pages = [
        _page(
            "https://example.com/news/future-event",
            f"展示会に出展します。開催日 {future}",
            title="展示会出展のお知らせ",
        ),
        _page(
            "https://example.com/news/past-event",
            f"展示会に出展しました。開催日 {past}",
            title="展示会出展実績",
        ),
    ]
    result = ProfileScorer(profile).score("Example", "https://example.com", pages)

    upcoming = _report(result, "exhibition_upcoming")
    recent = _report(result, "exhibition_recent")
    assert upcoming.earned is True
    assert "出展予定:" in upcoming.detail
    assert "future-event" in upcoming.detail
    assert recent.earned is True
    assert "出展実績:" in recent.detail
    assert "past-event" in recent.detail

    # 展示会EvidenceはR2正式100点へは加算しない。
    assert result.total_score == 0
    assert result.judgment == "C"


def test_exhibition_old_history_is_not_recent():
    profile = load_profile("t2_lab")
    old = (date.today() - timedelta(days=365 * 3)).isoformat()
    pages = [
        _page(
            "https://example.com/news/old-event",
            f"展示会に出展しました。開催日 {old}",
            title="過去の展示会",
        )
    ]
    result = ProfileScorer(profile).score("Example", "https://example.com", pages)
    assert _report(result, "exhibition_recent").earned is False


def test_contact_department_requires_both_contact_and_department():
    profile = load_profile("t2_lab")
    both = ProfileScorer(profile).score(
        "Example",
        "https://example.com",
        [_page("https://example.com/contact", "お問い合わせ 開発部 担当者")],
    )
    contact = _report(both, "contact_department_info")
    assert contact.earned is True

    contact_only = ProfileScorer(profile).score(
        "Example",
        "https://example.com",
        [_page("https://example.com/contact", "お問い合わせはこちら")],
    )
    assert _report(contact_only, "contact_department_info").earned is False
