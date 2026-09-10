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
