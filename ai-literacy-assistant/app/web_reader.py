import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .config import Settings
from .providers import ProviderError, SearchOutcome
from .schemas import Citation


PAGE_TEXT_LIMIT = 12000
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SUPPORTED_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
SKIP_TAGS = {"script", "style", "noscript"}
META_KEYS = {
    "description",
    "og:description",
    "og:title",
    "citation_title",
    "citation_author",
    "citation_publication_date",
    "citation_date",
    "citation_year",
    "citation_abstract",
}


class WebReader(Protocol):
    async def read(self, url: str) -> SearchOutcome: ...


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.metadata: dict[str, list[str]] = {}
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            values = {key.lower(): value or "" for key, value in attrs}
            name = (values.get("name") or values.get("property") or "").strip().lower()
            content = values.get("content", "").strip()
            if name in META_KEYS and content:
                entries = self.metadata.setdefault(name, [])
                if content not in entries:
                    entries.append(content[:4000])

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        else:
            self.text_parts.append(text)

    @property
    def visible_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text_parts)).strip()[:PAGE_TEXT_LIMIT]

    @property
    def document_title(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.title_parts)).strip()


class PublicWebReader:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.settings = settings
        self.transport = transport

    async def _resolve_addresses(self, hostname: str, port: int) -> list[str]:
        try:
            values = await asyncio.get_running_loop().getaddrinfo(
                hostname, port, type=socket.SOCK_STREAM
            )
        except socket.gaierror as exc:
            raise ProviderError("公开网页域名无法解析") from exc
        return list(dict.fromkeys(value[4][0].split("%", 1)[0] for value in values))

    async def _validate_url(self, url: str) -> str:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ProviderError("公开网页 URL 格式无效") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            raise ProviderError("仅支持公开的 HTTP/HTTPS 网页")
        if parsed.username is not None or parsed.password is not None:
            raise ProviderError("公开网页 URL 不能包含用户凭据")
        if any(ord(character) < 32 for character in url):
            raise ProviderError("公开网页 URL 格式无效")

        normalized_host = hostname.rstrip(".").lower()
        if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
            raise ProviderError("公开网页地址不是公网地址")
        try:
            addresses = [str(ipaddress.ip_address(normalized_host.split("%", 1)[0]))]
        except ValueError:
            addresses = await self._resolve_addresses(
                normalized_host, port or (443 if parsed.scheme.lower() == "https" else 80)
            )
        if not addresses:
            raise ProviderError("公开网页域名没有可用地址")
        for address in addresses:
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError as exc:
                raise ProviderError("公开网页域名解析结果无效") from exc
            if not parsed_address.is_global:
                raise ProviderError("公开网页地址不是公网地址")
        return urlunsplit(
            (parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, "")
        )

    @staticmethod
    def _decode_body(body: bytes, content_type_header: str) -> str:
        charset_match = re.search(
            r"(?:^|;)\s*charset\s*=\s*[\"']?([A-Za-z0-9._-]+)",
            content_type_header,
            flags=re.IGNORECASE,
        )
        encoding = charset_match.group(1) if charset_match else ""
        if not encoding:
            meta_match = re.search(
                br"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
                body[:4096],
                flags=re.IGNORECASE,
            )
            encoding = meta_match.group(1).decode("ascii") if meta_match else "utf-8"
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")

    @staticmethod
    def _citation(html: str, final_url: str) -> Citation:
        parser = _PageParser()
        parser.feed(html)
        parser.close()
        metadata = parser.metadata

        def first(*keys: str) -> str:
            for key in keys:
                values = metadata.get(key)
                if values:
                    return values[0]
            return ""

        page_text = parser.visible_text
        title = (
            first("citation_title")
            or parser.document_title
            or first("og:title")
            or final_url
        )[:200]
        authors = metadata.get("citation_author", [])
        publication_date = first(
            "citation_publication_date", "citation_date", "citation_year"
        )
        abstract = first("citation_abstract", "description", "og:description")
        details: list[str] = []
        if authors:
            details.append("作者：" + "、".join(authors[:10]))
        if publication_date:
            details.append("发表日期：" + publication_date)
        if abstract:
            details.append("摘要：" + abstract)
        snippet = "；".join(details)[:900] if details else page_text[:900]
        if not snippet:
            snippet = "页面未提供可提取的正文或论文元数据"
        return Citation(
            title=title,
            url=final_url,
            snippet=snippet,
            kind="webpage",
            page_text=page_text,
        )

    async def _read(self, url: str) -> SearchOutcome:
        current_url = url
        redirects = 0
        timeout = httpx.Timeout(self.settings.web_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
            headers={
                "Accept": "text/html, application/xhtml+xml",
                "User-Agent": "AILiteracyPublicWebReader/1.0",
            },
        ) as client:
            while True:
                current_url = await self._validate_url(current_url)
                client.cookies.clear()
                async with client.stream("GET", current_url) as response:
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location", "").strip()
                        if not location:
                            raise ProviderError("公开网页重定向缺少目标地址")
                        if redirects >= 3:
                            raise ProviderError("公开网页重定向次数过多")
                        redirects += 1
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status_code >= 400:
                        raise ProviderError(f"公开网页返回 HTTP {response.status_code}")

                    content_type_header = response.headers.get("content-type", "")
                    content_type = content_type_header.split(";", 1)[0].strip().lower()
                    if content_type not in SUPPORTED_CONTENT_TYPES:
                        raise ProviderError("公开网页不是可读取的 HTML 页面")
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > self.settings.web_max_bytes:
                                raise ProviderError("公开网页内容超过大小限制")
                        except ValueError:
                            pass

                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.settings.web_max_bytes:
                            raise ProviderError("公开网页内容超过大小限制")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    html = self._decode_body(body, content_type_header)
                    return SearchOutcome(results=[self._citation(html, current_url)])

    async def read(self, url: str) -> SearchOutcome:
        try:
            async with asyncio.timeout(self.settings.web_timeout_seconds):
                return await self._read(url)
        except ProviderError:
            raise
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderError("公开网页读取超时") from exc
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ProviderError("公开网页读取失败") from exc


def build_web_reader(settings: Settings) -> PublicWebReader | None:
    if not settings.web_reader_enabled or settings.demo_mode:
        return None
    return PublicWebReader(settings)
