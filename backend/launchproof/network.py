"""Bounded, robots-aware public HTTPS reader with address-pinned TLS.

No cookies, credentials, private addresses, arbitrary ports or proxy inheritance.
Still deploy workers behind an egress firewall in production.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

USER_AGENT = "LaunchProofResearch/0.1"
MAX_BYTES = 1_000_000


class FetchError(Exception):
    pass


def public_target(url: str) -> tuple[str, str]:
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise FetchError("Only public HTTPS URLs without credentials, on port 443, are allowed")
    host = p.hostname.encode("idna").decode("ascii")
    if len(host) > 253 or any(c in host for c in "\r\n\x00%"):
        raise FetchError("Invalid hostname")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError("DNS resolution failed") from exc
    ips = sorted({item[4][0] for item in addresses})
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise FetchError("Private, reserved or mixed public/private DNS targets are blocked")
    return host, ips[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, timeout=12, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the validated address, not a second DNS resolution.
        sock = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    text: str


def request(url: str, redirects: int = 0) -> tuple[int, dict, bytes, str]:
    if redirects > 3:
        raise FetchError("Too many redirects")
    host, address = public_target(url)
    p = urlsplit(url)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    conn = PinnedHTTPS(host, address)
    try:
        conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        response = conn.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        if response.status in (301, 302, 303, 307, 308):
            location = headers.get("location")
            if not location:
                raise FetchError("Redirect missing Location")
            target = urljoin(url, location)
            # Do not transfer requests across origins silently.
            if urlsplit(target).netloc.lower() != p.netloc.lower():
                raise FetchError("Cross-origin redirect blocked; review the final URL separately")
            return request(target, redirects + 1)
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise FetchError("Page exceeds size limit")
        if headers.get("content-encoding", "identity") != "identity":
            raise FetchError("Compressed content is not supported")
        return response.status, headers, body, url
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise FetchError(type(exc).__name__) from exc
    finally:
        conn.close()


class PublicWeb:
    def __init__(self):
        self.robots: dict[str, RobotFileParser] = {}

    def fetch(self, url: str) -> Page:
        public_target(url)
        p = urlsplit(url)
        origin = f"https://{p.netloc}"
        if origin not in self.robots:
            status, _, body, _ = request(origin + "/robots.txt")
            parser = RobotFileParser()
            if status == 404:
                parser.parse([])
            elif status == 200:
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
            else:
                raise FetchError(f"Cannot establish robots permission: HTTP {status}")
            self.robots[origin] = parser
        if not self.robots[origin].can_fetch(USER_AGENT, url):
            raise FetchError("robots.txt disallows this page")
        status, headers, body, final_url = request(url)
        if status != 200:
            raise FetchError(f"HTTP {status}; authentication and paywalls are not bypassed")
        mime = headers.get("content-type", "").split(";")[0].strip()
        if mime not in ("text/html", "text/plain", "application/xhtml+xml"):
            raise FetchError("Unsupported page content type")
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else final_url
        for tag in soup(["script", "style", "nav", "footer", "noscript", "form"]):
            tag.decompose()
        return Page(final_url, title[:300], soup.get_text(" ", strip=True)[:16000])
