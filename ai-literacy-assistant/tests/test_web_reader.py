import asyncio

import httpx
import pytest

from app.providers import ProviderError
from app.web_reader import PAGE_TEXT_LIMIT, PublicWebReader


PUBLIC_ADDRESS = "93.184.216.34"


def make_reader(settings, monkeypatch, handler, addresses=None):
    reader = PublicWebReader(settings, transport=httpx.MockTransport(handler))

    async def resolve(hostname, port):
        return addresses or [PUBLIC_ADDRESS]

    monkeypatch.setattr(reader, "_resolve_addresses", resolve)
    return reader


def html_response(html="<html><body>公开正文</body></html>", status=200, headers=None):
    response_headers = {"content-type": "text/html; charset=utf-8"}
    response_headers.update(headers or {})
    return httpx.Response(status, headers=response_headers, content=html.encode("utf-8"))


@pytest.mark.asyncio
async def test_extracts_paper_metadata_and_cleans_html(settings, monkeypatch):
    html = """
    <html><head>
      <title>备用标题</title>
      <meta name="citation_title" content="真实论文题名">
      <meta name="citation_author" content="张三">
      <meta name="citation_author" content="李四">
      <meta name="citation_publication_date" content="2025-06-01">
      <meta name="citation_abstract" content="这是论文摘要">
      <script>恶意脚本不得进入正文</script>
      <style>样式不得进入正文</style>
    </head><body><p>公开正文</p><p>""" + ("文" * PAGE_TEXT_LIMIT) + """</p></body></html>
    """
    reader = make_reader(settings, monkeypatch, lambda request: html_response(html))
    outcome = await reader.read("https://public.test/paper")
    citation = outcome.results[0]
    assert citation.kind == "webpage"
    assert citation.title == "真实论文题名"
    assert citation.url == "https://public.test/paper"
    assert "张三、李四" in citation.snippet
    assert "2025-06-01" in citation.snippet
    assert "这是论文摘要" in citation.snippet
    assert citation.page_text.startswith("公开正文")
    assert len(citation.page_text) == PAGE_TEXT_LIMIT
    assert "恶意脚本" not in citation.page_text
    assert "样式不得" not in citation.page_text
    assert "page_text" not in citation.model_dump()
    assert "公开正文" not in repr(citation)


@pytest.mark.asyncio
async def test_manual_redirect_revalidates_and_drops_cookies(settings, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        assert "cookie" not in request.headers
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"location": "/final", "set-cookie": "session=secret"},
            )
        return html_response("<html><body>最终页面</body></html>")

    reader = make_reader(settings, monkeypatch, handler)
    outcome = await reader.read("https://public.test/start")
    assert outcome.results[0].url == "https://public.test/final"
    assert outcome.results[0].page_text == "最终页面"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_rejects_redirect_to_private_address(settings, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    reader = make_reader(settings, monkeypatch, handler)
    with pytest.raises(ProviderError, match="公网地址"):
        await reader.read("https://public.test/start")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_rejects_excessive_redirects(settings, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": f"/hop-{len(requests)}"})

    reader = make_reader(settings, monkeypatch, handler)
    with pytest.raises(ProviderError, match="重定向次数过多"):
        await reader.read("https://public.test/start")
    assert len(requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "https://user:pass@public.test/paper",
    "http://localhost/paper",
    "http://127.0.0.1/paper",
    "http://10.0.0.8/paper",
    "http://[::1]/paper",
])
async def test_rejects_unsafe_urls_before_http(settings, monkeypatch, url):
    def handler(request):
        raise AssertionError("不安全 URL 不应发起 HTTP 请求")

    reader = make_reader(settings, monkeypatch, handler)
    with pytest.raises(ProviderError):
        await reader.read(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "::1"])
async def test_rejects_non_public_dns_results(settings, monkeypatch, address):
    def handler(request):
        raise AssertionError("非公网 DNS 结果不应发起 HTTP 请求")

    reader = make_reader(settings, monkeypatch, handler, addresses=[address])
    with pytest.raises(ProviderError, match="公网地址"):
        await reader.read("https://public.test/paper")


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"content-type": "application/pdf"}])
async def test_rejects_missing_or_unsupported_content_type(settings, monkeypatch, headers):
    reader = make_reader(
        settings,
        monkeypatch,
        lambda request: httpx.Response(200, headers=headers, content=b"not-html"),
    )
    with pytest.raises(ProviderError, match="HTML"):
        await reader.read("https://public.test/document")


@pytest.mark.asyncio
async def test_rejects_large_content_length(settings, monkeypatch):
    settings.web_max_bytes = 1024
    reader = make_reader(
        settings,
        monkeypatch,
        lambda request: html_response(headers={"content-length": "2048"}),
    )
    with pytest.raises(ProviderError, match="大小限制"):
        await reader.read("https://public.test/large")


class LargeStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"x" * 600
        yield b"x" * 600


@pytest.mark.asyncio
async def test_enforces_limit_while_streaming(settings, monkeypatch):
    settings.web_max_bytes = 1024

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/html"}, stream=LargeStream()
        )

    reader = make_reader(settings, monkeypatch, handler)
    with pytest.raises(ProviderError, match="大小限制"):
        await reader.read("https://public.test/stream")


@pytest.mark.asyncio
async def test_timeout_and_http_errors_are_provider_errors(settings, monkeypatch):
    settings.web_timeout_seconds = 0.01

    async def slow_handler(request):
        await asyncio.sleep(0.05)
        return html_response()

    reader = make_reader(settings, monkeypatch, slow_handler)
    with pytest.raises(ProviderError, match="超时"):
        await reader.read("https://public.test/slow")

    settings.web_timeout_seconds = 1
    reader = make_reader(settings, monkeypatch, lambda request: html_response(status=403))
    with pytest.raises(ProviderError, match="HTTP 403"):
        await reader.read("https://public.test/forbidden")
