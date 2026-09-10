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


def test_parent_company_site_does_not_score_for_subsidiary():
    profile = load_profile("t2_lab")
    today = date.today().isoformat()
    pages = [
        _page(
            "https://www.tel.co.jp/",
            "東京エレクトロン株式会社 製品情報 装置 電子回路設計 SoC AI技術 "
            "開発拠点 新製品 発売 " + today,
            title="東京エレクトロン株式会社",
        ),
        _page(
            "https://www.tel.co.jp/news/event/2025/example.html",
            "東京エレクトロン株式会社 回路設計 SoC AI技術 開発拠点",
            title="SEMICON | 東京エレクトロン株式会社",
        ),
    ]
    result = ProfileScorer(profile).score(
        "東京エレクトロン宮城株式会社",
        "https://www.tel.co.jp/",
        pages,
    )

    assert result.total_score == 0
    assert result.judgment == "C"
    assert all(not axis.earned for axis in result.axes)
    assert all("対象会社スコープ外Evidence除外" in axis.detail for axis in result.axes)


def test_parent_site_scores_only_target_company_subpage():
    profile = load_profile("t2_lab")
    pages = [
        _page(
            "https://www.tel.co.jp/",
            "東京エレクトロン株式会社 電子回路設計 SoC AI技術 開発拠点",
            title="東京エレクトロン株式会社",
        ),
        _page(
            "https://www.tel.co.jp/about/locations/tml.html",
            "東京エレクトロン宮城株式会社 自社製品 装置 開発部",
            title="東京エレクトロン宮城株式会社 | 企業情報 | 東京エレクトロン株式会社",
        ),
    ]
    result = ProfileScorer(profile).score(
        "東京エレクトロン宮城株式会社",
        "https://www.tel.co.jp/",
        pages,
    )

    assert result.total_score == 30
    assert result.judgment == "C"
    axes = {axis.id: axis for axis in result.axes}
    assert axes["own_product_manufacturer"].earned is True
    assert axes["development_department"].earned is True
    assert axes["circuit_design"].earned is False
    assert axes["fpga_soc_development"].earned is False
    assert axes["ai_vision"].earned is False


def test_exhibition_upcoming_and_recent_are_report_only():
    profile = load_profile("t2_lab")
    future = (date.today() + timedelta(days=90)).isoformat()
    past = (date.today() - timedelta(days=120)).isoformat()
    pages = [
        _page(
            "https://example.com/news/future-event",
            f"Example株式会社は「CEATEC 2026」に出展します。会期 {future}",
            title="CEATEC 2026 出展のお知らせ",
        ),
        _page(
            "https://example.com/news/past-event",
            f"Example株式会社は「EdgeTech+ 2025」に出展しました。開催日 {past}",
            title="EdgeTech+ 2025 出展報告",
        ),
    ]
    result = ProfileScorer(profile).score("Example株式会社", "https://example.com", pages)

    upcoming = _report(result, "exhibition_upcoming")
    recent = _report(result, "exhibition_recent")
    assert upcoming.earned is True
    assert "出展予定:" in upcoming.detail
    assert "展示会:CEATEC 2026" in upcoming.detail
    assert "対象会社:一致" in upcoming.detail
    assert "future-event" in upcoming.detail
    assert recent.earned is True
    assert "出展実績:" in recent.detail
    assert "展示会:EdgeTech+ 2025" in recent.detail
    assert "past-event" in recent.detail

    assert result.total_score == 0
    assert result.judgment == "C"


def test_exhibition_old_history_is_not_recent():
    profile = load_profile("t2_lab")
    old = (date.today() - timedelta(days=365 * 3)).isoformat()
    pages = [
        _page(
            "https://example.com/news/old-event",
            f"Example株式会社は展示会に出展しました。開催日 {old}",
            title="過去の展示会",
        )
    ]
    result = ProfileScorer(profile).score("Example株式会社", "https://example.com", pages)
    assert _report(result, "exhibition_recent").earned is False


def test_upcoming_article_publish_date_is_not_counted_as_past_exhibition():
    profile = load_profile("t2_lab")
    published = (date.today() - timedelta(days=14)).isoformat()
    future = (date.today() + timedelta(days=45)).isoformat()
    pages = [
        _page(
            "https://example.com/news/expo",
            f"Example株式会社｜公開日 {published}。「複合材料・カーボンフェア2026」に出展します。会期 {future}",
            title="展示会出展のご案内「複合材料・カーボンフェア2026」",
        )
    ]
    result = ProfileScorer(profile).score("Example株式会社", "https://example.com", pages)
    upcoming = _report(result, "exhibition_upcoming")
    recent = _report(result, "exhibition_recent")

    assert upcoming.earned is True
    assert future in upcoming.detail
    assert "複合材料・カーボンフェア2026" in upcoming.detail
    assert recent.earned is False


def test_past_announcement_is_not_confirmed_exhibition_history():
    profile = load_profile("t2_lab")
    past = (date.today() - timedelta(days=60)).isoformat()
    pages = [
        _page(
            "https://example.com/news/whx",
            f"Example株式会社は「WHX Osaka 2026」に共同出展いたします。開催日 {past}",
            title="展示会出展のご案内「WHX Osaka 2026」",
        )
    ]
    result = ProfileScorer(profile).score("Example株式会社", "https://example.com", pages)
    recent = _report(result, "exhibition_recent")

    assert recent.earned is False
    assert "過去出展告知:" in recent.detail
    assert "WHX Osaka 2026" in recent.detail
    assert "実績未確認" in recent.detail


def test_quoted_event_name_without_exhibition_word_is_extracted():
    profile = load_profile("t2_lab")
    past = (date.today() - timedelta(days=90)).isoformat()
    pages = [
        _page(
            "https://example.com/news/smart-sensing",
            f"Example株式会社は「Smart Sensing 2026」に出展しました。開催日 {past}",
            title="Smart Sensing 2026 出展報告",
        )
    ]
    result = ProfileScorer(profile).score("Example株式会社", "https://example.com", pages)
    recent = _report(result, "exhibition_recent")

    assert recent.earned is True
    assert "展示会:Smart Sensing 2026" in recent.detail


def test_thank_you_page_counts_as_confirmed_exhibition_history():
    profile = load_profile("t2_lab")
    past = (date.today() - timedelta(days=180)).isoformat()
    pages = [
        _page(
            "https://example.com/news/jimtof",
            f"Example株式会社 第32回 日本国際工作機械見本市(JIMTOF2024)、弊社ブースにご来場くださいましてありがとうございました。開催日 {past}",
            title="第32回 日本国際工作機械見本市(JIMTOF2024)、弊社ブースにご来場くださいましてありがとうございました。",
        )
    ]
    result = ProfileScorer(profile).score("Example株式会社", "https://example.com", pages)
    recent = _report(result, "exhibition_recent")

    assert recent.earned is True
    assert "出展実績:" in recent.detail
    assert "日本国際工作機械見本市" in recent.detail


def test_exhibition_requires_target_company_name_match():
    profile = load_profile("t2_lab")
    future = (date.today() + timedelta(days=30)).isoformat()
    pages = [
        _page(
            "https://www.tel.co.jp/news/expo",
            f"東京エレクトロン株式会社は「CEATEC 2026」に出展します。会期 {future}",
            title="CEATEC 2026 出展のお知らせ",
        )
    ]
    result = ProfileScorer(profile).score(
        "東京エレクトロン宮城株式会社",
        "https://www.tel.co.jp",
        pages,
    )
    upcoming = _report(result, "exhibition_upcoming")
    assert upcoming.earned is False
    assert "参考候補" in upcoming.detail
    assert "対象会社:一致未確認" in upcoming.detail


def test_shop_opening_is_not_exhibition_evidence():
    profile = load_profile("t2_lab")
    future = (date.today() + timedelta(days=30)).isoformat()
    pages = [
        _page(
            "https://example.com/news/shop",
            f"Example株式会社が新店舗を出店します。オープン予定日 {future}",
            title="新店舗出店のお知らせ",
        )
    ]
    result = ProfileScorer(profile).score("Example株式会社", "https://example.com", pages)
    assert _report(result, "exhibition_upcoming").earned is False
    assert _report(result, "exhibition_recent").earned is False


def test_exhibition_list_page_is_not_used_as_event_detail():
    profile = load_profile("t2_lab")
    past = (date.today() - timedelta(days=30)).isoformat()
    pages = [
        _page(
            "https://example.com/news/page/2",
            f"Example株式会社 {past} その他のお知らせ 展示会 ご来場ありがとうございました 出展",
            title="お知らせ",
        )
    ]
    result = ProfileScorer(profile).score("Example株式会社", "https://example.com", pages)
    recent = _report(result, "exhibition_recent")
    assert recent.earned is False
    assert "出展実績:" not in recent.detail


def test_event_title_is_used_instead_of_body_fragment():
    profile = load_profile("t2_lab")
    past = (date.today() - timedelta(days=60)).isoformat()
    pages = [
        _page(
            "https://example.com/news/jpca2026",
            f"Example株式会社 開催時期：{past} 展示会に出展しました。電動式ズームレンズを展示しました。",
            title="電子機器トータルソリューション展2026 出展報告 - Example株式会社",
        )
    ]
    result = ProfileScorer(profile).score("Example株式会社", "https://example.com", pages)
    recent = _report(result, "exhibition_recent")
    assert recent.earned is True
    assert "展示会:電子機器トータルソリューション展2026" in recent.detail
    assert "ムレンズ" not in recent.detail


def test_year_exhibition_page_can_confirm_past_history():
    profile = load_profile("t2_lab")
    past = (date.today() - timedelta(days=180)).isoformat()
    pages = [
        _page(
            "https://example.com/jp/technology/event/y2026",
            f"イリソ電子工業株式会社 {past} に開催された「CES 2026」に出展いたしました。",
            title="2026年展示会情報｜イリソ電子工業株式会社",
        )
    ]
    result = ProfileScorer(profile).score(
        "イリソ電子工業株式会社", "https://example.com", pages
    )
    recent = _report(result, "exhibition_recent")
    assert recent.earned is True
    assert "展示会:CES 2026" in recent.detail


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


def test_multi_event_year_page_uses_latest_confirmed_event():
    profile = load_profile("t2_lab")
    older = date.today() - timedelta(days=120)
    newer = date.today() - timedelta(days=30)

    pages = [
        _page(
            "https://example.com/jp/technology/event/y2026",
            (
                f'Example株式会社 {older.isoformat()} に開催された'
                f'「OLD EXPO」に出展いたしました。 '
                f'Example株式会社 {newer.isoformat()} に開催された'
                f'「NEW EXPO」に出展いたしました。'
            ),
            title="2026年展示会情報｜Example株式会社",
        )
    ]

    result = ProfileScorer(profile).score(
        "Example株式会社",
        "https://example.com",
        pages,
    )
    recent = _report(result, "exhibition_recent")

    assert recent.earned is True
    assert newer.isoformat() in recent.detail
    assert "NEW EXPO" in recent.detail


def test_yearless_event_date_is_inferred_from_event_title():
    profile = load_profile("t2_lab")
    event_date = date.today() - timedelta(days=20)
    published = event_date - timedelta(days=50)

    pages = [
        _page(
            "https://example.com/news/expo",
            (
                f"Example株式会社 {published.isoformat()} "
                f"展示会に出展いたします。"
                f"会期 {event_date.month}月{event_date.day}日 "
            ),
            title=f"TEST EXPO {event_date.year} 出展のご案内",
        )
    ]

    result = ProfileScorer(profile).score(
        "Example株式会社",
        "https://example.com",
        pages,
    )
    recent = _report(result, "exhibition_recent")

    assert recent.earned is False
    assert "過去出展告知:" in recent.detail
    assert event_date.isoformat() in recent.detail


def test_recruit_news_category_is_not_event_detail():
    profile = load_profile("t2_lab")
    past = (date.today() - timedelta(days=30)).isoformat()

    pages = [
        _page(
            "https://example.com/recruit-news/recruit-news_category/event",
            (
                f'Example株式会社 {past} '
                f'「Smart Sensing 2026」に出展しました。'
            ),
            title="イベント",
        )
    ]

    result = ProfileScorer(profile).score(
        "Example株式会社",
        "https://example.com",
        pages,
    )

    assert _report(
        result,
        "exhibition_recent",
    ).earned is False
