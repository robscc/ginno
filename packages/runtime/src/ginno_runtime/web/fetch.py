"""Safe page fetch + readable-text extraction (citations-design.md §4.2).

Guards: http/https only; the host is resolved ONCE and every resolved address
must be public (a mixed answer is refused outright); the connection is then
**pinned to one of those validated addresses** — the socket connects directly
to the validated IP, so there is no second, attacker-raceable DNS lookup
(TOCTOU/DNS-rebinding safe). Redirects are followed manually (bounded) and
EACH hop re-runs the resolve+guard before a single byte is fetched from it.

stdlib HTMLParser-based text extraction (zero new deps — a stronger extractor
can be slotted in lazily later).
"""

from __future__ import annotations

import html as _html
import http.client
import ipaddress
import re
import socket
import ssl
import urllib.parse
from html.parser import HTMLParser

from ..lang import t

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MAX_REDIRECTS = 5
MAX_BODY_BYTES = 1_500_000

_FETCHABLE_TAGS = {
    "p", "div", "section", "article", "li", "ul", "ol", "table", "tr", "td",
    "th", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "br",
    "main", "header", "footer", "figure", "figcaption", "dt", "dd",
}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "head", "nav", "form"}


class FetchError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _FETCHABLE_TAGS and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._skip_depth == 0 and data.strip():
            self.parts.append(data)


def _split_url(url: str) -> urllib.parse.SplitResult:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as e:
        raise FetchError(t(f"Failed to parse URL: {url}", f"URL 解析失败: {url}")) from e
    if parts.scheme not in ("http", "https"):
        raise FetchError(t(f"Only http/https is supported (got {parts.scheme or 'empty'} scheme)", f"仅支持 http/https（收到 {parts.scheme or '空'} 协议）"))
    if not parts.hostname:
        raise FetchError(t("URL has no host", "URL 缺少主机名"))
    return parts


def _resolve_public(host: str, port: int) -> list:
    """Resolve *host* ONCE and require EVERY answer to be a public address.

    A mixed answer (public + private) is refused outright: we would connect to
    one of these addresses, and an attacker-controlled resolver can interleave
    a private IP to bypass a check-then-fetch guard. Returns the addrinfo list.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchError(t(f"DNS resolution failed: {host}", f"域名解析失败: {host}")) from e
    if not infos:
        raise FetchError(t(f"DNS resolution failed: {host}", f"域名解析失败: {host}"))
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_reserved
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise FetchError(t(f"Refusing to access intranet/loopback address: {host} -> {ip}", f"拒绝访问内网/本机地址: {host} -> {ip}"))
    return infos


def _assert_openable_host(url: str) -> str:
    """Guard for handing a URL to the OS BROWSER (/api/open-external).

    No DNS probe. The browser resolves and connects under ITS own network
    stack — our getaddrinfo answering fake-IP (Clash/Surge fake-ip mode,
    198.18.0.0/15 = ipaddress ``is_reserved``) made every external link read
    as「内网/本机」and click-dead (2026-11-03: github.com -> 198.18.0.21).
    Syntax-level checks stay: http/https only; literal IPs and localhost
    names still get the intranet/loopback refusal. Symbolic hostnames pass —
    an internal hostname opened IN THE BROWSER is the browser's origin model
    to police, not our SSRF surface (that is web_fetch's resolve-then-pin).
    """
    parts = _split_url(url)
    host = (parts.hostname or "").strip().lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise FetchError(t("Refusing to access intranet/loopback address: localhost", "拒绝访问内网/本机地址: localhost"))
    try:
        ip = ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        return url  # symbolic hostname — the browser resolves it
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_reserved
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise FetchError(t(f"Refusing to access intranet/loopback address: {host} -> {ip}", f"拒绝访问内网/本机地址: {host} -> {ip}"))
    return url


def _get_pinned(url: str, timeout_s: float) -> tuple[str, str, bytes]:
    """GET *url* on a socket pinned to the validated address.

    Follows redirects manually (≤ MAX_REDIRECTS), re-running resolve+guard for
    every hop. Returns ``(final_url, content_type, body)``.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = _split_url(current)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = _resolve_public(host, port)

        conn = None
        last_err: Exception | None = None
        for info in infos:
            family, socktype, proto, _, sockaddr = info
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(timeout_s)
            try:
                sock.connect(sockaddr)
                if parts.scheme == "https":
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
                cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
                conn = cls(host, port, timeout=timeout_s)
                conn.sock = sock  # pin: bypass DNS entirely, use validated address
                break
            except OSError as e:
                last_err = e
                try:
                    sock.close()
                except OSError:
                    pass
                conn = None
        if conn is None:
            raise FetchError(t(f"Connection failed: {host}: {last_err}", f"连接失败: {host}: {last_err}"))

        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            conn.request(
                "GET",
                path,
                headers={
                    "Host": host,
                    "User-Agent": _UA,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Connection": "close",
                },
            )
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location") or ""
                try:
                    resp.read(0)
                except Exception:
                    pass
                if not loc:
                    raise FetchError(t("Redirect missing Location", "重定向缺少 Location"))
                current = urllib.parse.urljoin(current, loc)
                continue
            if resp.status >= 400:
                raise FetchError(f"HTTP {resp.status}")
            body = resp.read(MAX_BODY_BYTES)
            return current, (resp.headers.get("Content-Type") or ""), body
        except FetchError:
            raise
        except Exception as e:
            raise FetchError(t(f"Fetch failed: {type(e).__name__}: {e}", f"抓取失败: {type(e).__name__}: {e}")) from e
        finally:
            try:
                conn.close()
            except Exception:
                pass
    raise FetchError(t(f"Too many redirects (> {MAX_REDIRECTS})", f"重定向次数过多（> {MAX_REDIRECTS}）"))


def fetch_page(url: str, timeout_s: float = 15.0, max_chars: int = 20000) -> dict:
    """GET *url* and return ``{"url", "final_url", "title", "text", "truncated"}``.

    Raises :class:`FetchError` on guard violations or transport failures —
    the tool layer maps it to the builtin "[error] …" contract.
    """
    final_url, content_type, raw = _get_pinned(url, timeout_s)
    text = raw.decode("utf-8", "replace")
    if "html" in content_type.lower() or text.lstrip()[:200].lower().find("<html") >= 0:
        ex = _TextExtractor()
        try:
            ex.feed(text)
        except Exception:
            ex.parts = [re.sub(r"<[^>]+>", " ", text)]
        body = re.sub(r"[ \t]+", " ", "".join(ex.parts))
        body = re.sub(r"\n\s*\n+", "\n\n", body).strip()
        title = _html.unescape(ex.title.strip())
    else:
        body, title = text.strip(), ""
    truncated = len(body) > max_chars
    return {
        "url": url,
        "final_url": final_url,
        "title": title,
        "text": body[:max_chars],
        "truncated": truncated,
    }
