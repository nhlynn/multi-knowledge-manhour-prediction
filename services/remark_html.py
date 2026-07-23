"""Remark HTML handling for MHES.

The Preview screen's "Remark:" field is a rich-text editor (Quill)
whose content is saved/exported as HTML. This module provides:

- ``sanitize_remark_html``: a whitelist-based sanitizer (defense-in-depth
  server-side check; the editor already constrains what it can produce,
  but the export endpoint has no auth and should not trust the client).
- ``remark_html_to_lines``: converts sanitized remark HTML into a list of
  "lines" (one per paragraph/list item/manual break), each a list of
  (text, format) runs. Bold/italic/underline/font-color and bullet/
  numbered-list markers are preserved per character via openpyxl rich
  text.
- ``build_single_cell_data``: merges all lines into ONE cell's worth of
  rich text (lines joined by line breaks, wrapped by the export route's
  cell formatting). xlsx only allows a single background fill and a
  single hyperlink per whole cell, so if the remark uses more than one
  highlight color or more than one link, only the first of each applies
  to the entire cell — everything else about the formatting is exact.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont

_ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "s",
    "ul", "ol", "li", "a", "span", "div", "blockquote",
}
_DANGEROUS_TAGS = {"script", "style", "iframe", "object", "embed", "svg", "form", "input", "button"}
_SAFE_URL_RE = re.compile(r"^(https?://|mailto:)", re.IGNORECASE)
_LIST_TYPES = {"bullet", "ordered", "checked", "unchecked"}
_COLOR_RE = re.compile(r"(?<!background-)color\s*:\s*(#[0-9a-fA-F]{3,6}|rgba?\([\d.,\s]+\))")
_BG_RE = re.compile(r"background-color\s*:\s*(#[0-9a-fA-F]{3,6}|rgba?\([\d.,\s]+\))")

HYPERLINK_COLOR = "FF0563C1"


class _RemarkSanitizer(HTMLParser):
    """Strips everything except a small whitelist of formatting tags/attrs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip_stack:
            if tag in _DANGEROUS_TAGS:
                self._skip_stack.append(tag)
            return
        if tag in _DANGEROUS_TAGS:
            self._skip_stack.append(tag)
            return
        if tag == "br":
            self.out.append("<br>")
            return
        if tag not in _ALLOWED_TAGS:
            return
        safe_attrs = self._safe_attrs(tag, dict(attrs))
        attr_str = "".join(f' {k}="{v}"' for k, v in safe_attrs.items())
        self.out.append(f"<{tag}{attr_str}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._skip_stack and tag.lower() == "br":
            self.out.append("<br>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_stack:
            if self._skip_stack[-1] == tag:
                self._skip_stack.pop()
            return
        if tag in _ALLOWED_TAGS and tag != "br":
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._skip_stack and data:
            self.out.append(escape(data))

    def _safe_attrs(self, tag: str, attrs: dict[str, str | None]) -> dict[str, str]:
        result: dict[str, str] = {}
        if tag == "a":
            href = (attrs.get("href") or "").strip()
            if href and _SAFE_URL_RE.match(href):
                result["href"] = escape(href, quote=True)
                result["target"] = "_blank"
                result["rel"] = "noopener noreferrer"
        elif tag == "span":
            style = attrs.get("style") or ""
            color_m = _COLOR_RE.search(style)
            bg_m = _BG_RE.search(style)
            parts = [m.group(0) for m in (color_m, bg_m) if m]
            if parts:
                result["style"] = escape("; ".join(parts), quote=True)
        elif tag == "li":
            # Quill always wraps both bullet AND numbered lists in <ol>
            # internally, distinguishing them only via this attribute on
            # each <li> (see ListContainer.tagName = 'OL' in Quill's
            # source) — losing it means every list renders as numbered.
            data_list = (attrs.get("data-list") or "").strip()
            if data_list in _LIST_TYPES:
                result["data-list"] = data_list
        return result

    def get_sanitized(self) -> str:
        return "".join(self.out)


def sanitize_remark_html(html: str) -> str:
    """Whitelist-sanitize remark HTML to prevent stored/reflected XSS."""
    if not html or not html.strip():
        return ""
    parser = _RemarkSanitizer()
    parser.feed(html)
    parser.close()
    return parser.get_sanitized()


class _RemarkLineParser(HTMLParser):
    """Splits sanitized remark HTML into lines of (text, format) runs.

    ``format`` keys: bold, italic, underline, color, background, href
    (all CSS-ish raw values; conversion to openpyxl types happens later).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[list[tuple[str, dict]]] = []
        self._current: list[tuple[str, dict]] = []
        self._started = False
        self._bold = 0
        self._italic = 0
        self._underline = 0
        self._color_stack: list[str | None] = []
        self._bg_stack: list[str | None] = []
        self._href_stack: list[str | None] = []
        self._list_stack: list[dict] = []

    def _fmt(self) -> dict:
        return {
            "bold": self._bold > 0,
            "italic": self._italic > 0,
            "underline": self._underline > 0,
            "color": self._color_stack[-1] if self._color_stack else None,
            "background": self._bg_stack[-1] if self._bg_stack else None,
            "href": self._href_stack[-1] if self._href_stack else None,
        }

    def _emit(self, text: str) -> None:
        if text != "":
            self._current.append((text, self._fmt()))

    def _break_line(self) -> None:
        self.lines.append(self._current)
        self._current = []

    def _start_block(self) -> None:
        if self._started:
            self._break_line()
        self._started = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        if tag in ("b", "strong"):
            self._bold += 1
        elif tag in ("i", "em"):
            self._italic += 1
        elif tag == "u":
            self._underline += 1
        elif tag == "a":
            self._href_stack.append(attrs_d.get("href"))
        elif tag == "span":
            style = attrs_d.get("style") or ""
            color_m = _COLOR_RE.search(style)
            bg_m = _BG_RE.search(style)
            self._color_stack.append(color_m.group(1) if color_m else None)
            self._bg_stack.append(bg_m.group(1) if bg_m else None)
        elif tag == "ul":
            self._list_stack.append({"tag": "ul", "counter": 0})
        elif tag == "ol":
            self._list_stack.append({"tag": "ol", "counter": 0})
        elif tag == "li":
            self._start_block()
            # Quill always uses <ol> for both bullet and numbered lists —
            # the actual type lives on the <li> itself (see
            # _RemarkSanitizer._safe_attrs) — so that takes priority over
            # the container tag, which is only a fallback for hand-authored
            # semantic HTML that never went through Quill.
            list_type = attrs_d.get("data-list")
            if list_type not in ("bullet", "ordered", "checked", "unchecked"):
                container_tag = self._list_stack[-1]["tag"] if self._list_stack else "ul"
                list_type = "ordered" if container_tag == "ol" else "bullet"

            if list_type == "ordered":
                if self._list_stack:
                    self._list_stack[-1]["counter"] += 1
                    n = self._list_stack[-1]["counter"]
                else:
                    n = 1
                self._emit(f"{n}. ")
            elif list_type == "checked":
                self._emit("☑ ")
            elif list_type == "unchecked":
                self._emit("☐ ")
            else:
                self._emit("• ")
        elif tag in ("p", "div", "blockquote"):
            self._start_block()
        elif tag == "br":
            self._break_on_br()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._break_on_br()

    def _break_on_br(self) -> None:
        # Quill represents an intentionally blank line as <p><br></p> — the
        # paragraph boundary (_start_block) already broke to a fresh empty
        # line, so breaking AGAIN here for the <br> itself would double up
        # into two blank lines instead of one. Only break if there's actual
        # content to split away from (a genuine mid-paragraph soft break).
        if self._current:
            self._break_line()
        self._started = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("b", "strong"):
            self._bold = max(0, self._bold - 1)
        elif tag in ("i", "em"):
            self._italic = max(0, self._italic - 1)
        elif tag == "u":
            self._underline = max(0, self._underline - 1)
        elif tag == "a":
            if self._href_stack:
                self._href_stack.pop()
        elif tag == "span":
            if self._color_stack:
                self._color_stack.pop()
            if self._bg_stack:
                self._bg_stack.pop()
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if data.strip() == "":
            # Whitespace-only text nodes only arise from formatting
            # whitespace between block tags (real Quill output never
            # inserts them) — drop those, but keep a single meaningful
            # space between inline elements (e.g. "text <b>bold</b>").
            if "\n" in data or "\t" in data:
                return
            data = " "
        else:
            data = re.sub(r"[ \t\r\n]+", " ", data)
        self._emit(data)

    def get_lines(self) -> list[list[tuple[str, dict]]]:
        if self._current or not self.lines:
            self.lines.append(self._current)
            self._current = []
        lines = self.lines
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return lines


def _to_hex_color(css_color: str | None) -> str | None:
    """Convert a CSS color (hex or rgb()) to an opaque 8-digit ARGB hex.

    openpyxl's Color/InlineFont/PatternFill only left-pad a bare 6-digit
    RGB hex with "00" (fully transparent) rather than "FF" (opaque) — see
    openpyxl.styles.colors.Color — so a plain "E60000" silently renders as
    invisible/no-color in Excel. Returning the full "FFE60000" ARGB form
    here avoids that trap everywhere this function is used (font color
    and cell fill alike).
    """
    if not css_color:
        return None
    css_color = css_color.strip()
    rgb_hex = None
    if css_color.startswith("#"):
        hex_part = css_color[1:]
        if len(hex_part) == 3:
            hex_part = "".join(c * 2 for c in hex_part)
        if len(hex_part) == 6:
            rgb_hex = hex_part.upper()
    else:
        m = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*[\d.]+\s*)?\)", css_color)
        if m:
            r, g, b = (int(x) for x in m.groups())
            rgb_hex = f"{r:02X}{g:02X}{b:02X}"
    return f"FF{rgb_hex}" if rgb_hex else None


def remark_html_to_lines(sanitized_html: str) -> list[list[tuple[str, dict]]]:
    """Parse sanitized remark HTML into lines of (text, format) runs.

    Returns an empty list if the remark has no visible content.
    """
    if not sanitized_html or not sanitized_html.strip():
        return []

    parser = _RemarkLineParser()
    parser.feed(sanitized_html)
    parser.close()
    lines = parser.get_lines()

    if not any(any(t.strip() for t, _ in line) for line in lines):
        return []
    return lines


def _first_background(runs: list[tuple[str, dict]]) -> str | None:
    for _, fmt in runs:
        bg_hex = _to_hex_color(fmt.get("background"))
        if bg_hex:
            return bg_hex
    return None


def _runs_to_blocks(runs: list[tuple[str, dict]]) -> list:
    """Expand (text, fmt) runs into openpyxl rich-text blocks.

    Links get an inline " (url)" text annotation appended (a whole cell can
    only carry one real hyperlink, handled separately by the caller).
    """
    blocks: list = []
    for run_text, fmt in runs:
        href = fmt.get("href")
        expanded = [(run_text, fmt)]
        if href:
            expanded.append((f" ({href})", {**fmt, "href": None}))

        for text, f in expanded:
            is_link_text = bool(f.get("href"))
            underline = f.get("underline") or is_link_text
            color_hex = HYPERLINK_COLOR if is_link_text else _to_hex_color(f.get("color"))
            if not (f.get("bold") or f.get("italic") or underline or color_hex):
                blocks.append(text)
                continue
            font_kwargs: dict = {}
            if f.get("bold"):
                font_kwargs["b"] = True
            if f.get("italic"):
                font_kwargs["i"] = True
            if underline:
                font_kwargs["u"] = "single"
            if color_hex:
                font_kwargs["color"] = color_hex
            blocks.append(TextBlock(InlineFont(**font_kwargs), text))
    return blocks


def build_single_cell_data(lines: list[list[tuple[str, dict]]]) -> dict | None:
    """Merge all parsed lines into ONE cell's worth of rich text.

    Lines are joined with line breaks within the same cell (wrap_text
    handles the visual wrapping). Since a single cell can only carry one
    fill color and one hyperlink for its entire content, this uses the
    first highlight color and first link found across all lines/runs —
    everything else (bold/italic/underline/font color/list markers) is
    still preserved exactly, per character.

    Returns ``None`` if there is no visible content.
    """
    if not lines:
        return None

    blocks: list = []
    fill = None
    hyperlink = None

    for i, line in enumerate(lines):
        if i > 0:
            _append_newline(blocks)
        runs = [(t, f) for t, f in line if t != ""]
        if not runs:
            continue
        if fill is None:
            fill = _first_background(runs)
        if hyperlink is None:
            for _, fmt in runs:
                if fmt.get("href"):
                    hyperlink = fmt["href"]
                    break
        blocks.extend(_runs_to_blocks(runs))

    if not blocks:
        return None
    return {"value": CellRichText(blocks), "fill": fill, "hyperlink": hyperlink}


def _append_newline(blocks: list) -> None:
    """Append a line break onto the end of the previous run's text.

    A run whose entire content is just "\\n" never gets marked
    xml:space="preserve" by openpyxl (its whitespace-preservation check
    only fires when a run has OTHER, non-whitespace content too — see
    openpyxl.xml.functions.whitespace), so a standalone newline block gets
    silently collapsed by Excel and list items/paragraphs run together on
    one line. Attaching it to the previous run's tail avoids that.
    """
    if not blocks:
        blocks.append("\n")
        return
    last = blocks[-1]
    if isinstance(last, str):
        blocks[-1] = last + "\n"
    else:
        last.text = last.text + "\n"
