"""URL normalization and crawl policy, ported from OpenSEO url-utils.ts / url-policy.ts.

``normalize_url`` reproduces what the WHATWG URL parser plus ``searchParams.sort()``
gave upstream, so URLs dedupe the same way. Hostname checks here run before DNS;
the resolved address is checked again at connect time (``net.GuardedBackend``).
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
from urllib.parse import unquote_to_bytes, urljoin, urlsplit

BLOCKED_HOSTS = frozenset(
    {"localhost", "metadata.google.internal", "metadata", "169.254.169.254", "100.100.100.200"}
)
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".home.arpa")
_DEFAULT_PORTS = {"http": 80, "https": 443}
# WHATWG path percent-encode set, minus the ASCII that never reaches a path.
_PATH_UNSAFE = frozenset(' "<>`{}')
_C0_AND_SPACE = "".join(chr(code) for code in range(0x21))
_FORM_SAFE = frozenset(b"*-._0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


class TargetBlocked(Exception):
    """The crawl target is private, internal, or otherwise not allowed."""


def normalize_host(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.split("%", 1)[0].removesuffix(".")


def is_public_ip(value: str) -> bool:
    """True only for globally routable unicast addresses, with IPv4 embeddings unwrapped."""
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(
            normalize_host(value)
        )
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = addr.ipv4_mapped or (addr.sixtofour) or (addr.teredo[1] if addr.teredo else None)
        if embedded is None and addr in ipaddress.IPv6Network("64:ff9b::/96"):
            embedded = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        if embedded is not None:
            addr = embedded
    return bool(
        addr.is_global
        and not (
            addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_private
        )
    )


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def is_blocked_host(hostname: str | None) -> bool:
    host = normalize_host(hostname or "")
    if not host or host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES):
        return True
    return _is_ip_literal(host) and not is_public_ip(host)


def _encode_path(path: str) -> str:
    out: list[str] = []
    for char in path:
        if char in _PATH_UNSAFE or ord(char) < 0x20 or ord(char) >= 0x7F:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8", "surrogatepass"))
        else:
            out.append(char)
    return "".join(out)


def _remove_dot_segments(path: str) -> str:
    if "/." not in path and not path.startswith("."):
        return path
    trailing = path.endswith(("/", "/.", "/.."))
    resolved = posixpath.normpath(path)
    if resolved.startswith("//"):
        resolved = "/" + resolved.lstrip("/")
    return resolved + "/" if trailing and resolved != "/" else resolved


def _form_decode(value: str) -> str:
    return unquote_to_bytes(value.replace("+", " ")).decode("utf-8", "replace")


def _form_encode(value: str) -> str:
    out: list[str] = []
    for byte in value.encode("utf-8", "surrogatepass"):
        if byte in _FORM_SAFE:
            out.append(chr(byte))
        elif byte == 0x20:
            out.append("+")
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def _sorted_query(query: str) -> str:
    """``URLSearchParams.sort()`` then serialize, as WHATWG does."""
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.append((_form_decode(name), _form_decode(value)))
    pairs.sort(key=lambda pair: pair[0])
    return "&".join(f"{_form_encode(name)}={_form_encode(value)}" for name, value in pairs)


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Resolve against ``base``; http(s) only, no fragment, sorted query, lowercase host.

    Trailing slashes are kept: ``/path`` and ``/path/`` stay distinct, so a CMS
    redirect to the slash form resolves instead of looping.
    """
    raw = re.sub(r"[\t\n\r]", "", url.strip(_C0_AND_SPACE))
    raw = raw.replace("\\", "/")
    try:
        if base is not None:
            raw = urljoin(base.replace("\\", "/"), raw)
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORTS or not parts.hostname:
            return None
        host = parts.hostname.lower()
        if not _is_ip_literal(host) and not host.isascii():
            host = host.encode("idna").decode("ascii")
        port = parts.port
    except (ValueError, UnicodeError):
        return None
    if ":" in host:
        host = f"[{host}]"
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = (userinfo + "@" if userinfo else "") + host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc += f":{port}"
    path = _encode_path(_remove_dot_segments(parts.path or "/"))
    query = _sorted_query(parts.query) if parts.query else ""
    return f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")


def _effective_port(scheme: str, port: int | None) -> int:
    return port if port is not None else _DEFAULT_PORTS.get(scheme, 0)


def is_same_origin(url: str, origin: str) -> bool:
    """Same host (``www.`` equivalent) and port; an http:80 origin also allows https:443."""
    try:
        target, base = urlsplit(url), urlsplit(origin)
        target_port, base_port = target.port, base.port
    except ValueError:
        return False
    host_a, host_b = (target.hostname or "").lower(), (base.hostname or "").lower()
    if not host_a or not (
        host_a == host_b or host_a == f"www.{host_b}" or host_b == f"www.{host_a}"
    ):
        return False
    a_scheme, b_scheme = target.scheme.lower(), base.scheme.lower()
    a_port, b_port = _effective_port(a_scheme, target_port), _effective_port(b_scheme, base_port)
    if a_scheme == b_scheme:
        return a_port == b_port
    return b_scheme == "http" and a_scheme == "https" and b_port == 80 and a_port == 443


def is_crawlable_url(url: str) -> bool:
    """Pre-DNS check for discovered URLs: http(s), no credentials, no internal host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in _DEFAULT_PORTS or parts.username or parts.password:
        return False
    return not is_blocked_host(parts.hostname)


def validate_start_url(value: str) -> str:
    """Normalize a start URL; ``ValueError`` when malformed, ``TargetBlocked`` when internal."""
    raw = value.strip()
    if not raw:
        raise ValueError("A start URL is required")
    if not raw.lower().startswith(("http://", "https://")):
        if "://" in raw:
            raise ValueError("Only http and https URLs can be audited")
        raw = "https://" + raw
    normalized = normalize_url(raw)
    if normalized is None or len(normalized) > 2048:
        raise ValueError("Not a valid http(s) URL")
    parts = urlsplit(normalized)
    if parts.username or parts.password:
        raise ValueError("A start URL cannot contain credentials")
    if not is_crawlable_url(normalized):
        raise TargetBlocked(normalized)
    return normalized


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    port = parts.port
    netloc = parts.hostname or ""
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if port is not None and port != _DEFAULT_PORTS.get(parts.scheme):
        netloc += f":{port}"
    return f"{parts.scheme}://{netloc}"


def parse_link_header_canonical(header: str | None, page_url: str) -> str | None:
    """``Link: <url>; rel="canonical"`` from a response header, normalized."""
    if not header:
        return None
    for part in header.split(","):
        match = re.search(r"<([^>]+)>\s*;(.*)", part, re.S)
        if match and re.search(r'rel\s*=\s*"?canonical"?', match.group(2), re.I):
            return normalize_url(match.group(1).strip(), page_url)
    return None


# A leading path segment that names a locale ("en", "zh-cn", "pt_BR"), not a section.
_LOCALE_SEGMENT = re.compile(r"^[a-z]{2}(?:[-_][a-z0-9]{2,4})?$", re.I)


def path_cluster(url: str) -> str:
    """The page template a URL most likely shares with its siblings, from its path shape.

    ``/products/bikes/strider-12`` -> ``/products/*/*``. A top-level page is its own
    cluster, a leading locale segment is dropped so translations share one, and a
    query string marks a variant (``/products?``). A heuristic: it only spreads the
    crawl budget across sections, nothing is judged by it.
    """
    parts = urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments and _LOCALE_SEGMENT.match(segments[0]):
        segments = segments[1:]
    if len(segments) <= 1:
        key = "/" + "".join(segments)
    else:
        key = "/" + "/".join([segments[0], *["*"] * (len(segments) - 1)])
    return key + "?" if parts.query else key
