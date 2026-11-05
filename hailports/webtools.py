"""Read-only page fetch, web search, and news feeds. Page text is cleaned before use."""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_SECRET_LINE = re.compile(r"(?i)(api[_-]?key|secret|password|token|seed phrase|private key)\s*[:=]")


class DataLine(ValueError):
    """Text looks like a secret or a private record. Do not send it to a model."""


def refuse_sensitive(text: str) -> None:
    if "BEGIN PRIVATE KEY" in text or "BEGIN OPENSSH PRIVATE KEY" in text:
        raise DataLine("private key")
    if _SECRET_LINE.search(text):
        raise DataLine("secret assignment")
    words = text.strip().split()
    if len(words) in (12, 24) and all(w.isalpha() and w.islower() for w in words):
        raise DataLine("possible seed phrase")


def sanitize_text(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    text = _LONG_HEX.sub("[token]", text)
    return text


def _shared_space():
    # Carrier-grade range, built without a dotted literal so the source stays plain.
    return ipaddress.IPv4Network((100 << 24 | 64 << 16, 10))


def _public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in _shared_space():
        return False
    return True


def _numeric_trick(host: str) -> bool:
    labels = host.split(".")
    if any(part.lower().startswith("0x") for part in labels):
        return True
    if labels and all(part.isdigit() for part in labels):
        if len(labels) != 4:
            return True
        if any(len(part) > 1 and part.startswith("0") for part in labels):
            return True
    return False


def _addresses_for(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return [literal]
    if _numeric_trick(host):
        raise DataLine("url host is not a public address")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DataLine("url host did not resolve") from exc
    found = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            found.append(ipaddress.ip_address(raw))
        except ValueError as exc:
            raise DataLine("url host is not a public address") from exc
    if not found:
        raise DataLine("url host did not resolve")
    return found


def public_http_url(url: str) -> bool:
    """True only for http(s) hosts whose addresses are all public.

    Loopback, private, link-local, and shared-carrier addresses are refused.
    A redirect is checked again by the caller. Name lookup failure is a refusal.
    """
    try:
        parsed = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip("[]").lower().rstrip(".")
    if not host:
        return False
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = _addresses_for(host, port)
    except DataLine:
        return False
    return all(_public_ip(ip) for ip in addresses)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _open_once(url: str, timeout: float, method: str, data: bytes | None, headers: dict[str, str]):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    return opener.open(req, timeout=timeout)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    text = " ".join(" ".join(parser.parts).split())
    return sanitize_text(text)


def http_get(url: str, timeout: float = 20.0, limit: int = 200_000) -> str:
    current = url
    headers = {"User-Agent": "hailports-webtools"}
    for _hop in range(4):
        if not public_http_url(current):
            raise DataLine("url is not a public http address")
        try:
            resp = _open_once(current, timeout, "GET", None, headers)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                loc = exc.headers.get("Location") or ""
                exc.close()
                if not loc:
                    raise DataLine("redirect had no location") from exc
                current = urllib.parse.urljoin(current, loc)
                continue
            raise RuntimeError("http " + str(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("the page did not answer") from exc
        with resp:
            raw = resp.read(limit)
            ctype = resp.headers.get("Content-Type", "")
        body = raw.decode("utf-8", "replace")
        if "html" in ctype or body.lstrip().lower().startswith("<!doctype") or "<html" in body[:200].lower():
            return html_to_text(body)
        return sanitize_text(body)
    raise DataLine("too many redirects")


def needs_web(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in ("look up", "search the web", "what is the latest", "news", "http://", "https://"))


_RESULT = re.compile(
    r'<div class="result">\s*<a href="([^"]+)">([^<]+)</a>\s*<p>([^<]*)</p>',
    re.I,
)


def parse_search_html(html: str) -> list[dict]:
    hits = []
    for url, title, snippet in _RESULT.findall(html or ""):
        hits.append({"title": sanitize_text(title.strip()), "url": url.strip(), "snippet": sanitize_text(snippet.strip())})
    return hits


def parse_feed(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter():
        tag = node.tag.lower().split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = ""
        link = ""
        summary = ""
        for child in list(node):
            child_tag = child.tag.lower().split("}")[-1]
            if child_tag == "title":
                title = (child.text or "").strip()
            elif child_tag == "link":
                link = (child.text or child.attrib.get("href") or "").strip()
            elif child_tag in ("description", "summary"):
                summary = (child.text or "").strip()
        if title:
            items.append({
                "title": sanitize_text(title),
                "url": link,
                "snippet": sanitize_text(summary),
            })
    return items


def _format_query(url_template: str, query: str) -> str:
    quoted = urllib.parse.quote_plus(query)
    if "{query}" in url_template:
        return url_template.replace("{query}", quoted)
    if url_template.endswith("=") or "query=" in url_template:
        return url_template + quoted
    joiner = "&" if "?" in url_template else "?"
    return url_template + joiner + "q=" + quoted


def search(query: str, url_template: str, fetch=http_get, limit: int = 5) -> list[dict]:
    """Fetch a configured search URL and parse the result blocks. No template means no hits."""
    if not url_template or not query.strip():
        return []
    body = fetch(_format_query(url_template, query))
    if "<rss" in body.lower() or "<feed" in body.lower():
        return parse_feed(body)[:limit]
    return parse_search_html(body)[:limit]


def news(feed_url: str, fetch=http_get, limit: int = 5) -> list[dict]:
    """Read an RSS or Atom feed. The feed URL is the one in config, not a built-in site."""
    if not feed_url:
        return []
    return parse_feed(fetch(feed_url))[:limit]


def gather_web(prompt: str, search_url: str = "", news_url: str = "", fetch=http_get) -> str:
    """Notes from pages, search, and news. The model writes the reply from these notes."""
    if not needs_web(prompt):
        return ""
    chunks: list[str] = []
    for url in re.findall(r"https?://[^\s]+", prompt)[:2]:
        if not public_http_url(url):
            continue
        try:
            chunks.append(fetch(url)[:4000])
        except Exception as exc:
            chunks.append("Page could not be read: " + str(exc)[:200])
    low = prompt.lower()
    if search_url and any(k in low for k in ("look up", "search the web", "what is the latest")):
        try:
            hits = search(prompt, search_url, fetch=fetch)
            chunks.extend(f"{hit['title']} {hit['url']} {hit['snippet']}".strip() for hit in hits)
        except Exception as exc:
            chunks.append("Search could not be read: " + str(exc)[:200])
    if news_url and "news" in low:
        try:
            chunks.extend(f"{hit['title']} {hit['url']}".strip() for hit in news(news_url, fetch=fetch))
        except Exception as exc:
            chunks.append("News could not be read: " + str(exc)[:200])
    if not chunks:
        return ""
    return "\n\nWeb notes:\n" + "\n".join(chunks)
