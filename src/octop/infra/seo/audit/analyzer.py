"""HTML page analysis, ported from OpenSEO page-analyzer.ts (htmlparser2 tokenizer).

The stdlib parser keeps no element stack, so this one mirrors htmlparser2's:
an end tag closes every element opened inside it, ``<body>`` implicitly closes
``<head>``, and unclosed elements close at the end of the document. Those
implicit closes are what end an open link anchor or H1 the way upstream does.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from octop.infra.seo.audit.urls import is_same_origin, normalize_url

_SKIPPED_LINK = re.compile(r"^(javascript:|mailto:|tel:|#)")
# Subtrees whose text is not visible content.
_NON_CONTENT = frozenset({"script", "style", "noscript", "svg"})
_HEADING_LEVELS = {f"h{level}": level for level in range(1, 7)}
MAX_ANCHOR_CHARS = 200
# Per-page caps; counts derived from these saturate on pathological pages.
MAX_EXTRACTED_LINKS = 1000
MAX_EXTRACTED_IMAGES = 1000
_APP_ROOT_IDS = frozenset({"root", "app", "__next", "__nuxt"})
_EXECUTABLE_SCRIPT_TYPES = frozenset({"", "module", "text/javascript", "application/javascript"})
# JavaScript's \s, which also covers the byte-order mark.
_JS_SPACE = "[\\s\\ufeff]"
# Elements the HTML parser keeps in <head>; any other element (or text) there starts the
# body early, and search engines ignore head-only tags that come after that point.
_HEAD_ELEMENTS = frozenset(
    {
        "html",
        "head",
        "title",
        "meta",
        "link",
        "script",
        "style",
        "base",
        "basefont",
        "bgsound",
        "noscript",
        "noframes",
        "template",
    }
)
MAX_JSON_LD_BLOCKS = 20

_VOID = frozenset(
    [
        "area",
        "base",
        "basefont",
        "br",
        "col",
        "command",
        "embed",
        "frame",
        "hr",
        "image",
        "img",
        "input",
        "keygen",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)
_FOREIGN = frozenset({"math", "svg"})
_HTML_INTEGRATION = frozenset(
    {"mi", "mo", "mn", "ms", "mtext", "annotation-xml", "foreignobject", "desc", "title"}
)
_P = frozenset({"p"})
_FORM = frozenset({"input", "option", "optgroup", "select", "button", "datalist", "textarea"})
_OPEN_IMPLIES_CLOSE: dict[str, frozenset[str]] = {
    "tr": frozenset({"tr", "th", "td"}),
    "th": frozenset({"th"}),
    "td": frozenset({"thead", "th", "td"}),
    "body": frozenset({"head", "link", "script"}),
    "li": frozenset({"li"}),
    "select": _FORM,
    "input": _FORM,
    "output": _FORM,
    "button": _FORM,
    "datalist": _FORM,
    "textarea": _FORM,
    "option": frozenset({"option"}),
    "optgroup": frozenset({"optgroup", "option"}),
    "dd": frozenset({"dd", "dt"}),
    "dt": frozenset({"dd", "dt"}),
    "rt": frozenset({"rt", "rp"}),
    "rp": frozenset({"rt", "rp"}),
    "tbody": frozenset({"thead", "tbody"}),
    "tfoot": frozenset({"thead", "tbody"}),
} | dict.fromkeys(
    [
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "address",
        "article",
        "aside",
        "blockquote",
        "details",
        "div",
        "dl",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "header",
        "hr",
        "main",
        "nav",
        "ol",
        "pre",
        "section",
        "table",
        "ul",
    ],
    _P,
)


def js_trim(text: str) -> str:
    return re.sub(rf"^{_JS_SPACE}+|{_JS_SPACE}+$", "", text)


def collapse_space(text: str) -> str:
    return js_trim(re.sub(rf"{_JS_SPACE}+", " ", text))


@dataclass
class Link:
    target_url: str
    anchor: str | None
    is_internal: bool
    is_nofollow: bool


@dataclass
class PageAnalysis:
    title: str = ""
    meta_description: str = ""
    canonical: str | None = None
    robots_meta: str | None = None
    og_title: str | None = None
    og_description: str | None = None
    og_image: str | None = None
    h1s: list[str] = field(default_factory=list)
    heading_order: list[int] = field(default_factory=list)
    word_count: int = 0
    body_text: str = ""
    # A conservative signal of an app shell, not proof of missing content.
    javascript_shell: bool = False
    images: list[dict[str, str | None]] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    has_structured_data: bool = False
    hreflang_tags: list[str] = field(default_factory=list)
    # Octop additions; the fields above keep upstream's shape.
    json_ld: list[str] = field(default_factory=list)
    # (hreflang, href as written) of each <link rel="alternate" hreflang>.
    alternates: list[tuple[str, str]] = field(default_factory=list)
    images_missing_size: int = 0
    # The element (or "#text") that ended <head> before <body>, if one did.
    head_broken_by: str | None = None
    # Head-only directives found after the body began: "canonical", "robots", ...
    misplaced_head: list[str] = field(default_factory=list)
    has_viewport: bool = False
    # The <html> element's lang attribute; None when it has none.
    html_lang: str | None = None
    declares_charset: bool = False
    has_icon: bool = False
    # The loading attribute of the first <img> in document order.
    first_image_loading: str | None = None
    # schema.org types named by microdata itemtype attributes.
    microdata_types: list[str] = field(default_factory=list)

    @property
    def content_hash(self) -> str | None:
        if not self.body_text or self.javascript_shell:
            return None
        return hashlib.sha256(self.body_text.encode("utf-8")).hexdigest()


class _Analyzer(HTMLParser):
    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.stack: list[str] = []
        self.foreign: list[bool] = []
        self.result = PageAnalysis()
        self.title: str | None = None
        self.title_depth = 0
        self.title_done = False
        # Element extraction stops inside <noscript>, as a scripting browser parses it.
        self.noscript_depth = 0
        self.meta_description: str | None = None
        self.has_app_root = False
        self.has_executable_script = False
        self.open_h1: list[str] | None = None
        self.links: dict[str, Link] = {}
        self.open_anchor: tuple[str, str, list[str]] | None = None
        self.suppress_depth = 0
        self.body_depth = 0
        self.head_depth = 0
        self.saw_body = False
        self.body_parts: list[str] = []
        self.fallback_parts: list[str] = []
        self.json_ld_parts: list[str] | None = None
        # The parser has left head content: <body> opened or a body element appeared.
        self.body_mode = False

    # htmlparser2's element stack -------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs)
        # HTML ignores the self-closing slash except in foreign (SVG/MathML) content.
        if self.foreign and self.foreign[-1] and self.stack and self.stack[-1] == tag:
            self.stack.pop()
            self._on_close(tag)

    def handle_endtag(self, tag: str) -> None:
        if (tag in _FOREIGN or tag in _HTML_INTEGRATION) and self.foreign:
            self.foreign.pop()
        if tag in _VOID or tag not in self.stack:
            return
        while self.stack:
            name = self.stack.pop()
            self._on_close(name)
            if name == tag:
                break

    def handle_data(self, data: str) -> None:
        self._on_text(data)

    def close(self) -> None:
        super().close()
        while self.stack:
            self._on_close(self.stack.pop())

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        implied = _OPEN_IMPLIES_CLOSE.get(tag)
        while implied and self.stack and self.stack[-1] in implied:
            self._on_close(self.stack.pop())
        if tag not in _VOID:
            self.stack.append(tag)
            if tag in _FOREIGN:
                self.foreign.append(True)
            elif tag in _HTML_INTEGRATION:
                self.foreign.append(False)
        attribs: dict[str, str] = {}
        for name, value in attrs:
            attribs.setdefault(name, value or "")
        self._on_open(tag, attribs)

    # page-analyzer.ts callbacks ---------------------------------------------------

    def _on_open(self, name: str, attribs: dict[str, str]) -> None:
        if name in _NON_CONTENT:
            self.suppress_depth += 1
        if name == "noscript":
            self.noscript_depth += 1
        if self.noscript_depth > 0:
            return
        if self.head_depth == 0 and self.suppress_depth == 0:
            self.has_app_root |= attribs.get("id") in _APP_ROOT_IDS or name == "app-root"
        itemtype = attribs.get("itemtype")
        if itemtype:
            for value in itemtype.split():
                type_name = value.rstrip("/").rsplit("/", 1)[-1]
                if type_name and type_name not in self.result.microdata_types:
                    self.result.microdata_types.append(type_name)
        if not self.body_mode and (name == "body" or name not in _HEAD_ELEMENTS):
            self.body_mode = True
            if name != "body":
                self.result.head_broken_by = name
        if name == "title":
            # Only the document title counts, never an SVG <title>.
            if not self.title_done and self.suppress_depth == 0:
                self.title_depth += 1
                if self.title is None:
                    self.title = ""
        elif name == "html":
            if self.result.html_lang is None and js_trim(attribs.get("lang") or ""):
                self.result.html_lang = js_trim(attribs["lang"])
        elif name == "head":
            self.head_depth += 1
        elif name == "body":
            self.body_depth += 1
            self.saw_body = True
        elif name == "meta":
            self._meta(attribs)
        elif name == "link":
            self._link(attribs)
        elif name == "img":
            if not self.result.images:
                self.result.first_image_loading = (attribs.get("loading") or "").lower() or None
            if len(self.result.images) < MAX_EXTRACTED_IMAGES:
                self.result.images.append({"src": attribs.get("src"), "alt": attribs.get("alt")})
                if not attribs.get("width") or not attribs.get("height"):
                    self.result.images_missing_size += 1
        elif name == "script":
            kind = attribs.get("type")
            self.has_executable_script |= (kind or "").lower() in _EXECUTABLE_SCRIPT_TYPES
            if kind == "application/ld+json":
                self.result.has_structured_data = True
            if (kind or "").strip().lower() == "application/ld+json" and (
                len(self.result.json_ld) < MAX_JSON_LD_BLOCKS
            ):
                self.json_ld_parts = []
        elif name == "a":
            # HTML forbids nested <a>; browsers close the open one.
            self._close_anchor()
            href = attribs.get("href")
            if href and not _SKIPPED_LINK.match(href):
                self.open_anchor = (href, (attribs.get("rel") or "").lower(), [])
        level = _HEADING_LEVELS.get(name)
        if level is not None:
            self.result.heading_order.append(level)
            if level == 1 and self.open_h1 is None:
                self.open_h1 = []

    def _on_text(self, text: str) -> None:
        if self.json_ld_parts is not None:
            self.json_ld_parts.append(text)
            return
        if self.suppress_depth > 0:
            return
        if self.title_depth > 0:
            if self.title is not None:
                self.title += text
            return
        if not self.body_mode and js_trim(text):
            self.body_mode = True
            self.result.head_broken_by = "#text"
        if self.open_h1 is not None:
            self.open_h1.append(text)
        if self.open_anchor is not None:
            self.open_anchor[2].append(text)
        if self.body_depth > 0:
            self.body_parts.append(text)
        elif self.head_depth == 0:
            self.fallback_parts.append(text)

    def _on_close(self, name: str) -> None:
        if name == "script" and self.json_ld_parts is not None:
            self.result.json_ld.append("".join(self.json_ld_parts))
            self.json_ld_parts = None
        if name in _NON_CONTENT and self.suppress_depth > 0:
            self.suppress_depth -= 1
        if name == "noscript" and self.noscript_depth > 0:
            self.noscript_depth -= 1
            return
        if self.noscript_depth > 0:
            return
        if name == "title" and self.title_depth > 0:
            self.title_depth -= 1
            if self.title_depth == 0:
                self.title_done = True
        if name == "head" and self.head_depth > 0:
            self.head_depth -= 1
        if name == "body" and self.body_depth > 0:
            self.body_depth -= 1
        if name == "a":
            self._close_anchor()
        if name == "h1" and self.open_h1 is not None:
            self.result.h1s.append(js_trim("".join(self.open_h1)))
            self.open_h1 = None

    def _meta(self, attribs: dict[str, str]) -> None:
        content = attribs.get("content")
        # Meta names are ASCII case-insensitive: "Description" is valid.
        name = (attribs.get("name") or "").lower()
        prop = attribs.get("property")
        result = self.result
        if "charset" in attribs or (
            (attribs.get("http-equiv") or "").lower() == "content-type"
            and "charset=" in (content or "").lower()
        ):
            result.declares_charset = True
        if name == "viewport":
            result.has_viewport = True
        elif name == "description":
            self._misplaced("description")
            if self.meta_description is None:
                self.meta_description = js_trim(content) if content is not None else ""
        elif name == "robots":
            if result.robots_meta is None:
                result.robots_meta = content
            self._misplaced("robots")
        elif prop == "og:title":
            if result.og_title is None:
                result.og_title = content
        elif prop == "og:description":
            if result.og_description is None:
                result.og_description = content
        elif prop == "og:image" and result.og_image is None:
            result.og_image = content

    def _misplaced(self, directive: str) -> None:
        if self.body_mode and directive not in self.result.misplaced_head:
            self.result.misplaced_head.append(directive)

    def _link(self, attribs: dict[str, str]) -> None:
        rel = attribs.get("rel")
        # "icon" and "shortcut icon" both name a favicon.
        if "icon" in (rel or "").lower().split():
            self.result.has_icon = True
        if rel == "canonical":
            self._misplaced("canonical")
            if self.result.canonical is None:
                self.result.canonical = attribs.get("href")
        elif rel == "alternate" and attribs.get("hreflang"):
            self._misplaced("hreflang")
            self.result.hreflang_tags.append(attribs["hreflang"])
            self.result.alternates.append((attribs["hreflang"], attribs.get("href") or ""))

    def _close_anchor(self) -> None:
        if self.open_anchor is None:
            return
        href, rel, text = self.open_anchor
        self.open_anchor = None
        if len(self.links) >= MAX_EXTRACTED_LINKS:
            return
        resolved = normalize_url(href, self.page_url)
        if not resolved or resolved in self.links:
            return
        anchor = collapse_space("".join(text))[:MAX_ANCHOR_CHARS]
        self.links[resolved] = Link(
            target_url=resolved,
            anchor=anchor or None,
            is_internal=is_same_origin(resolved, self.page_url),
            is_nofollow="nofollow" in re.split(_JS_SPACE, rel),
        )

    def finish(self) -> PageAnalysis:
        result = self.result
        result.title = js_trim(self.title or "")
        result.meta_description = self.meta_description or ""
        result.links = list(self.links.values())
        result.body_text = collapse_space(
            "".join(self.body_parts if self.saw_body else self.fallback_parts)
        )
        result.word_count = (
            len(re.split(rf"{_JS_SPACE}+", result.body_text)) if result.body_text else 0
        )
        result.javascript_shell = (
            self.has_app_root
            and self.has_executable_script
            and result.word_count < 20
            and not result.heading_order
            and not result.links
            and not result.images
        )
        return result


def analyze_html(html: str, page_url: str) -> PageAnalysis:
    """Extract title, metas, headings, images, links, structured data and visible text."""
    parser = _Analyzer(page_url)
    parser.feed(html)
    parser.close()
    return parser.finish()
