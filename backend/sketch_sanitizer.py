"""Conservative sanitizer for model-authored meeting sketches."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_ALLOWED_TAGS = frozenset(
    {
        "article",
        "aside",
        "b",
        "blockquote",
        "br",
        "caption",
        "code",
        "col",
        "colgroup",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "em",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "i",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "small",
        "span",
        "strong",
        "style",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)
_VOID_TAGS = frozenset({"br", "col", "hr"})
_DROP_CONTENT_TAGS = frozenset({"embed", "iframe", "object", "script"})
_ALLOWED_ATTRIBUTES = frozenset(
    {
        "aria-label",
        "aria-labelledby",
        "class",
        "colspan",
        "dir",
        "id",
        "lang",
        "role",
        "rowspan",
        "style",
        "title",
    }
)
_UNSAFE_CSS = re.compile(r"(?:@import|expression\s*\(|url\s*\(|javascript:)", re.IGNORECASE)
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; "
    "font-src 'none'; connect-src 'none'; media-src 'none'; frame-src 'none'; "
    "form-action 'none'; base-uri 'none'"
)


class _SketchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._blocked_depth = 0
        self._inside_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if self._blocked_depth:
            if normalized not in _VOID_TAGS:
                self._blocked_depth += 1
            return
        if normalized not in _ALLOWED_TAGS:
            if normalized in _DROP_CONTENT_TAGS:
                self._blocked_depth = 1
            return
        if normalized == "style":
            self._inside_style = True
        rendered: list[str] = []
        for name, value in attrs:
            attribute = name.lower()
            if attribute not in _ALLOWED_ATTRIBUTES or value is None:
                continue
            if attribute == "style" and _UNSAFE_CSS.search(value):
                continue
            rendered.append(f' {attribute}="{html.escape(value, quote=True)}"')
        self.parts.append(f"<{normalized}{''.join(rendered)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if self._blocked_depth:
            self._blocked_depth -= 1
            return
        if normalized in _ALLOWED_TAGS and normalized not in _VOID_TAGS:
            self.parts.append(f"</{normalized}>")
        if normalized == "style":
            self._inside_style = False

    def handle_data(self, data: str) -> None:
        if not self._blocked_depth:
            safe = "" if self._inside_style and _UNSAFE_CSS.search(data) else data
            self.parts.append(html.escape(safe))


def sanitize_sketch(document: str) -> str:
    """Return inert self-contained HTML with an explicit zero-egress CSP."""

    parser = _SketchParser()
    parser.feed(document)
    parser.close()
    content = "".join(parser.parts)
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
        "<style>html{color-scheme:light dark}body{font:14px system-ui;margin:16px}"
        "table{border-collapse:collapse}td,th{border:1px solid #888;padding:6px}"
        "pre{white-space:pre-wrap}</style></head><body>"
        f"{content}</body></html>"
    )
