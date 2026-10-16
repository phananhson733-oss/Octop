"""robots.txt rules, matching robots-parser 3.0.1 as OpenSEO discovery.ts uses it.

Longest matching pattern wins, Allow wins a tie, ``*`` and a trailing ``$`` are
wildcards. Our product token's group is used when present, else ``*``. As
upstream, rules apply only to URLs on the robots file's own scheme, host and
port; every other URL is allowed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

# RFC 9309 lets parsers stop at 500 KiB; Google does.
MAX_ROBOTS_BYTES = 500 * 1024
_ENCODE_SAFE = ";,/?:@&=+$!*'()#%"


def _product_token(user_agent: str) -> str:
    return user_agent.lower().split("/", 1)[0].strip()


def _upper_escapes(path: str) -> str:
    return re.sub(r"%[0-9a-fA-F]{2}", lambda match: match.group(0).upper(), path)


def _normalise_pattern(pattern: str) -> str:
    try:
        return _upper_escapes(quote(pattern, safe=_ENCODE_SAFE))
    except UnicodeEncodeError:
        return pattern


def _matches(pattern: str, path: str) -> bool:
    lengths = [0]
    for index, char in enumerate(pattern):
        if char == "$" and index + 1 == len(pattern):
            return lengths[-1] == len(path)
        if char == "*":
            lengths = list(range(lengths[0], len(path) + 1))
            continue
        lengths = [n + 1 for n in lengths if n < len(path) and path[n] == char]
        if not lengths:
            return False
    return True


def _scheme_host_port(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), port or (443 if scheme == "https" else 80)


@dataclass
class Robots:
    origin: str
    groups: dict[str, list[tuple[str, bool]]] = field(default_factory=dict)
    sitemaps: list[str] = field(default_factory=list)

    def is_allowed(self, url: str, user_agent: str) -> bool:
        target = _scheme_host_port(url)
        if target is None or target != _scheme_host_port(self.origin + "/robots.txt"):
            return True
        # An empty group of ours still wins over "*", as an empty array is truthy upstream.
        token = _product_token(user_agent)
        rules = self.groups[token] if token in self.groups else self.groups.get("*", [])
        parts = urlsplit(url)
        path = _upper_escapes((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
        best: tuple[str, bool] | None = None
        for pattern, allow in rules:
            if not _matches(pattern, path):
                continue
            if (
                best is None
                or len(pattern) > len(best[0])
                or len(pattern) == len(best[0])
                and allow
                and not best[1]
            ):
                best = (pattern, allow)
        return best is None or best[1]


def parse_robots(origin: str, text: str | None) -> Robots:
    """``None`` (missing or unreachable robots.txt) allows everything."""
    robots = Robots(origin)
    if text is None:
        return robots
    agents: list[str] = []
    after_agent_line = False
    for line in re.split(r"\r\n|\r|\n", text[:MAX_ROBOTS_BYTES]):
        key, sep, value = line.split("#", 1)[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if not sep or not key:
            continue
        if key == "user-agent":
            if not after_agent_line:
                agents = []
            if value:
                agents.append(_product_token(value))
        elif key in {"allow", "disallow"}:
            for agent in agents:
                group = robots.groups.setdefault(agent, [])
                if value:
                    group.append((_normalise_pattern(value), key == "allow"))
        elif key == "sitemap" and value:
            robots.sitemaps.append(value)
        after_agent_line = key == "user-agent"
    return robots
