"""Turning a Gmail message into the little text the workflow needs (M8).

Email content is untrusted and mostly noise: markup, tracking links, hidden
preheaders, legal footers. Everything here is plain code, so that what reaches
the LLM is only a trimmed text of the visible content (SPEC "Gmail Search
Behavior"). Nothing in this module logs or stores the text.
"""

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

# What the model may see of one email. Order details sit near the top of a
# receipt; the cut loses footers.
MAX_TEXT_CHARS = 6000
MAX_HEADER_CHARS = 200
# A plain-text part shorter than this is usually a stub ("view this email in a browser").
_MIN_PLAIN_CHARS = 200
_MAX_PART_DEPTH = 10

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# Zero-width and soft-hyphen characters, used as padding in marketing email.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\u034f"))
_SPACES = re.compile("[ \t\u00a0\u2007\u202f]+")
_CHARSET = re.compile(r"charset\s*=\s*\"?([A-Za-z0-9._-]+)", re.IGNORECASE)
# UTF-8 that was read as Latin-1 or Windows-1252 somewhere ("WagWellie\u00c2\u00ae" for
# "WagWellie\u00ae"): a UTF-8 lead byte's character followed by continuation bytes' characters,
# as either charset shows them.
_MOJIBAKE = re.compile(
    "[\u00c2-\u00f4][\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc"
    "\u2013\u2014\u2018-\u201a\u201c-\u201e\u2020-\u2022\u2026\u2030\u2039\u203a\u20ac\u2122]+"
)
_UTF8_NAMES = frozenset({"utf-8", "utf8"})
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)

_SKIPPED_TAGS = frozenset({"script", "style", "head", "title", "noscript", "svg", "template"})
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "wbr"})
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "hr", "tr", "li", "ul", "ol", "table", "section", "article",
        "header", "footer", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
    }
)  # fmt: skip
_CELL_TAGS = frozenset({"td", "th"})


@dataclass(frozen=True, slots=True)
class ParsedEmail:
    """One email, reduced to what a person would read. All of it is untrusted."""

    message_id: str
    thread_id: str | None
    sender: str
    subject: str
    received_at: datetime | None
    # Visible text only, trimmed and capped at `MAX_TEXT_CHARS`.
    text: str


def parse_message(resource: dict[str, Any]) -> ParsedEmail:
    """Reduce Gmail's `users.messages` resource (`format=full`) to a `ParsedEmail`."""
    payload = resource.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    plain: list[str] = []
    html: list[str] = []
    _collect(payload, plain, html, depth=0)
    text = trim_text("\n".join(plain))
    if len(text) < _MIN_PLAIN_CHARS and html:
        text = trim_text("\n".join(html_to_text(part) for part in html)) or text
    thread_id = resource.get("threadId")
    return ParsedEmail(
        message_id=str(resource.get("id", "")),
        thread_id=thread_id if isinstance(thread_id, str) else None,
        sender=_header(payload, "From"),
        subject=_header(payload, "Subject"),
        received_at=_internal_date(resource.get("internalDate")),
        text=text,
    )


def html_to_text(html: str) -> str:
    """The visible text of an HTML body, one block per line."""
    parser = _VisibleText()
    parser.feed(html)
    parser.close()
    return parser.text()


def repair_mojibake(text: str) -> str:
    """Undo UTF-8 that was decoded as Latin-1 or Windows-1252, one run at a time.

    A run is replaced only if its bytes are valid UTF-8, so real accented text is kept.
    """

    def fix(match: re.Match[str]) -> str:
        try:
            return b"".join(_byte(ch) for ch in match.group()).decode("utf-8")
        except UnicodeError:
            return match.group()

    return _MOJIBAKE.sub(fix, text)


def _byte(ch: str) -> bytes:
    return bytes([ord(ch)]) if ord(ch) < 256 else ch.encode("cp1252")


def trim_text(text: str) -> str:
    """Drop links, invisible padding and blank lines, and cap the length."""
    text = _URL.sub("", text.translate(_INVISIBLE))
    lines = []
    for line in text.splitlines():
        line = _SPACES.sub(" ", line).strip()
        # A line with no letter or digit is decoration ("-----", "|  |").
        if any(ch.isalnum() for ch in line):
            lines.append(line)
    return "\n".join(lines)[:MAX_TEXT_CHARS].rstrip()


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        # Open elements, each with whether it hides its content.
        self._open: list[tuple[str, bool]] = []
        self._hidden = 0
        self._mailto: list[str | None] = []

    def text(self) -> str:
        return "".join(self._chunks)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")
        elif tag in _CELL_TAGS:
            self._chunks.append(" ")
        if tag in _VOID_TAGS:
            return
        attributes = dict(attrs)
        hidden = tag in _SKIPPED_TAGS or bool(_HIDDEN_STYLE.search(attributes.get("style") or ""))
        self._open.append((tag, hidden))
        self._hidden += hidden
        if tag == "a":
            href = (attributes.get("href") or "").strip()
            is_mailto = href.lower().startswith("mailto:")
            self._mailto.append(href[7:].split("?", 1)[0] if is_mailto else None)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")
        if not any(name == tag for name, _ in self._open):
            return
        # Close everything left open inside it, as a browser would.
        while self._open:
            name, hidden = self._open.pop()
            self._hidden -= hidden
            if name == "a" and self._mailto:
                address = self._mailto.pop()
                # Keep a contact address that is only in the link ("Contact us").
                if address and not self._hidden and address not in self.text()[-200:]:
                    self._chunks.append(f" ({address})")
            if name == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self._chunks.append(data)


def _collect(part: dict[str, Any], plain: list[str], html: list[str], *, depth: int) -> None:
    """Gather the text bodies of a MIME tree, leaving out attachments."""
    if depth > _MAX_PART_DEPTH or part.get("filename"):
        return
    mime_type = str(part.get("mimeType", "")).lower()
    if mime_type in ("text/plain", "text/html"):
        body = part.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, str) and data:
            text = _decode(data, _header(part, "Content-Type", limit=None))
            (plain if mime_type == "text/plain" else html).append(text)
    parts = part.get("parts")
    if isinstance(parts, list):
        for child in parts:
            if isinstance(child, dict):
                _collect(child, plain, html, depth=depth + 1)


def _decode(data: str, content_type: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    # Bytes that are valid UTF-8 almost surely are UTF-8, whatever the header claims:
    # mislabelled charsets are common in shop templates.
    try:
        return repair_mojibake(raw.decode("utf-8"))
    except UnicodeDecodeError:
        pass
    match = _CHARSET.search(content_type)
    charset = match.group(1).lower() if match else "cp1252"
    if charset in _UTF8_NAMES:
        # Labelled UTF-8 but isn't: the commonest real charset.
        charset = "cp1252"
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("cp1252", errors="replace")
    return repair_mojibake(text)


def _header(part: dict[str, Any], name: str, *, limit: int | None = MAX_HEADER_CHARS) -> str:
    headers = part.get("headers")
    if not isinstance(headers, list):
        return ""
    for header in headers:
        if isinstance(header, dict) and str(header.get("name", "")).lower() == name.lower():
            value = " ".join(str(header.get("value", "")).translate(_INVISIBLE).split())
            value = repair_mojibake(value)
            return value if limit is None else value[:limit]
    return ""


def _internal_date(value: object) -> datetime | None:
    """Gmail's `internalDate`: milliseconds since the epoch, as a string."""
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None
