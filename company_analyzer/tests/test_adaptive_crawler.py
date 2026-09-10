from datetime import date

from company_analyzer.crawler.crawler import (
    _discover_candidate_links,
    _fetch_page_sync,
)


def test_discovers_relevant_nested_links_same_domain_only():
    html = """
    <html><body>
      <a href="/about/outline/executive/">組織・役員</a>
      <a href="/technology/fpga/">FPGA開発技術</a>
      <a href="/recruit/jobs/embedded/">組込み技術者採用</a>
      <a href="/news/exhibition/ceatec/">展示会出展情報</a>
      <a href="/events/material-fair/">複合材料フェア出展</a>
      <a href="/contact/">お問い合わせ</a>
      <a href="/store/tokyo/">新店舗を出店</a>
      <a href="/privacy/">プライバシー</a>
      <a href="https://other.example.com/products">他社製品</a>
      <a href="/catalog/test.pdf">PDF</a>
    </body></html>
    """
    links = _discover_candidate_links(
        html,
        "https://www.example.com/about/",
        "https://example.com/",
    )
    urls = [url for _, url in links]

    assert "https://www.example.com/about/outline/executive" in urls
    assert "https://www.example.com/technology/fpga" in urls
    assert "https://www.example.com/recruit/jobs/embedded" in urls
    assert "https://www.example.com/news/exhibition/ceatec" in urls
    assert "https://www.example.com/events/material-fair" in urls
    assert "https://www.example.com/contact" in urls
    assert not any("/store/tokyo" in url for url in urls)
    assert not any("privacy" in url for url in urls)
    assert not any("other.example.com" in url for url in urls)
    assert not any(url.endswith(".pdf") for url in urls)


def test_relevant_links_are_priority_sorted():
    html = """
    <a href="/company/">会社情報</a>
    <a href="/technology/fpga-linux/">FPGA 組込みLinux 開発</a>
    """
    links = _discover_candidate_links(
        html,
        "https://example.com/",
        "https://example.com/",
    )
    assert links[0][1] == "https://example.com/technology/fpga-linux"


def test_target_company_link_is_highest_priority_on_group_site():
    html = """
    <a href="/about/locations/tml.html">東京エレクトロン宮城株式会社</a>
    <a href="/news/event/semicon.html">SEMICON 展示会</a>
    <a href="/products/">製品情報</a>
    """
    links = _discover_candidate_links(
        html,
        "https://www.tel.co.jp/about/locations/",
        "https://www.tel.co.jp/",
        company_name="東京エレクトロン宮城株式会社",
    )
    assert links[0][1] == "https://www.tel.co.jp/about/locations/tml.html"


def test_news_archive_pagination_advances_only_one_page():
    html = """
    <a href="/news/page/2/">2</a>
    <a href="/news/page/3/">3</a>
    <a href="/news/page/5/">5</a>
    <a href="/news/page/46/">46</a>
    <a href="/privacy/">privacy</a>
    """
    links = _discover_candidate_links(
        html,
        "https://example.com/news/",
        "https://example.com/",
    )
    urls = [url for _, url in links]
    assert "https://example.com/news/page/2" in urls
    assert "https://example.com/news/page/3" not in urls
    assert "https://example.com/news/page/5" not in urls
    assert "https://example.com/news/page/46" not in urls
    assert not any("privacy" in url for url in urls)

    html2 = """
    <a href="/news/page/2/">2</a>
    <a href="/news/page/3/">3</a>
    <a href="/news/page/10/">10</a>
    """
    links2 = _discover_candidate_links(
        html2,
        "https://example.com/news/page/2/",
        "https://example.com/",
    )
    urls2 = [url for _, url in links2]
    assert "https://example.com/news/page/3" in urls2
    assert "https://example.com/news/page/10" not in urls2


def test_event_archive_year_navigation_is_recent_only():
    current_year = date.today().year
    previous = current_year - 1
    two_years_ago = current_year - 2
    old = current_year - 3
    html = f"""
    <a href="/technology/event/y{current_year}/">{current_year}年</a>
    <a href="/technology/event?year={previous}">{previous}年</a>
    <a href="/technology/event/y{two_years_ago}/">{two_years_ago}年</a>
    <a href="/technology/event/y{old}/">{old}年</a>
    <a href="/company/">会社情報</a>
    """
    links = _discover_candidate_links(
        html,
        "https://example.com/technology/event/",
        "https://example.com/",
    )
    urls = [url for _, url in links]
    assert f"https://example.com/technology/event/y{current_year}" in urls
    assert f"https://example.com/technology/event?year={previous}" in urls
    assert f"https://example.com/technology/event/y{two_years_ago}" in urls
    assert f"https://example.com/technology/event/y{old}" not in urls

    scores = {url: score for score, url in links}
    assert scores[f"https://example.com/technology/event/y{current_year}"] > scores[f"https://example.com/technology/event?year={previous}"]


def test_year_archive_keeps_relevant_dated_article_and_next_page():
    year = date.today().year
    html = f"""
    <a href="/{year}/08/26/9330/">自己株式処分のお知らせ</a>
    <a href="/{year}/06/08/9240/">JPCAショー{year} 電子機器トータルソリューション展に出展します</a>
    <a href="/{year}/page/2/">2</a>
    <a href="/{year}/page/5/">5</a>
    <a href="/products/jig/magicarrier_x/">ノンシリコーンタイプ粘着キャリア MagiCarrier-X 製品</a>
    <a href="/recruit/career/">キャリア採用情報</a>
    """
    links = _discover_candidate_links(
        html,
        f"https://example.com/{year}/",
        "https://example.com/",
    )
    urls = [url for _, url in links]
    scores = {url: score for score, url in links}

    article = f"https://example.com/{year}/06/08/9240"
    page2 = f"https://example.com/{year}/page/2"
    product = "https://example.com/products/jig/magicarrier_x"
    recruit = "https://example.com/recruit/career"

    assert article in urls
    assert page2 in urls
    assert f"https://example.com/{year}/08/26/9330" not in urls
    assert f"https://example.com/{year}/page/5" not in urls
    assert scores[article] > scores[page2]
    assert scores[page2] > scores[product]
    assert scores[page2] > scores[recruit]


def test_fetch_page_keeps_final_redirect_url():
    class FakeResponse:
        status_code = 200
        encoding = "utf-8"
        apparent_encoding = "utf-8"
        text = "<html><body>会社概要</body></html>"
        url = "https://example.com/jp/"

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    result = _fetch_page_sync(FakeSession(), "https://example.com/")
    assert result.status_code == 200
    assert result.url == "https://example.com/jp/"
