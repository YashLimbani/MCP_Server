"""Convert Stack Exchange HTML post bodies into compact Markdown (stdlib only).

Keeps what matters for an AI assistant (code blocks with indentation, inline code,
links, lists, headings, quotes) and drops everything else.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # decodes &quot; &gt; etc.
        self.out: list[str] = []
        self.nl = 0  # number of trailing newlines currently at the end of output
        self.in_pre = False
        self.lists: list[list] = []  # [ordered: bool, counter: int]
        self.links: list[tuple[str | None, int]] = []  # (href, start index in out)
        self.quotes: list[int] = []  # start index in out

    # --- output helpers -------------------------------------------------
    def _emit(self, text: str) -> None:
        if not text:
            return
        self.out.append(text)
        stripped = text.rstrip("\n")
        if stripped:
            self.nl = len(text) - len(stripped)
        else:
            self.nl += len(text)

    def _recount_nl(self) -> None:
        tail = "".join(self.out[-3:])
        self.nl = len(tail) - len(tail.rstrip("\n"))

    def _block_break(self) -> None:
        if self.out and self.nl < 2:
            self._emit("\n" * (2 - self.nl))

    def _line_break(self) -> None:
        if self.out and self.nl < 1:
            self._emit("\n")

    # --- parser callbacks -----------------------------------------------
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("p", "div", "table"):
            self._block_break()
        elif tag in _HEADINGS:
            self._block_break()
            self._emit("#" * int(tag[1]) + " ")
        elif tag == "pre":
            self._block_break()
            m = re.search(r"lang-([\w+#-]+)", a.get("class") or "")
            lang = m.group(1) if m and m.group(1) != "none" else ""
            self._emit(f"```{lang}\n")
            self.in_pre = True
        elif tag == "code":
            if not self.in_pre:
                self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "hr":
            self._block_break()
            self._emit("---")
            self._block_break()
        elif tag == "br":
            self._emit("\n")
        elif tag == "img":
            alt = a.get("alt")
            self._emit(f"[image: {alt}]" if alt else "[image]")
        elif tag in ("ul", "ol"):
            if self.lists:
                self._line_break()
            else:
                self._block_break()
            self.lists.append([tag == "ol", 0])
        elif tag == "li":
            self._line_break()
            indent = "  " * max(len(self.lists) - 1, 0)
            if self.lists and self.lists[-1][0]:
                self.lists[-1][1] += 1
                self._emit(f"{indent}{self.lists[-1][1]}. ")
            else:
                self._emit(f"{indent}- ")
        elif tag == "blockquote":
            self._block_break()
            self.quotes.append(len(self.out))
        elif tag == "a":
            self.links.append((a.get("href"), len(self.out)))

    def handle_endtag(self, tag):
        if tag in ("p", "div", "table") or tag in _HEADINGS:
            self._block_break()
        elif tag == "pre":
            self._line_break()
            self._emit("```")
            self.in_pre = False
            self._block_break()
        elif tag == "code":
            if not self.in_pre:
                self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            if self.lists:
                self._line_break()
            else:
                self._block_break()
        elif tag == "li":
            self._line_break()
        elif tag in ("td", "th"):
            self._emit(" ")
        elif tag == "tr":
            self._line_break()
        elif tag == "blockquote":
            if not self.quotes:
                return
            start = self.quotes.pop()
            segment = "".join(self.out[start:]).strip("\n")
            del self.out[start:]
            if segment:
                lines = [("> " + ln) if ln.strip() else ">" for ln in segment.split("\n")]
                self.out.append("\n".join(lines))
            self._recount_nl()
            self._block_break()
        elif tag == "a":
            if not self.links:
                return
            href, start = self.links.pop()
            segment = "".join(self.out[start:]).strip()
            del self.out[start:]
            if segment:
                if href and segment != href:
                    segment = f"[{segment}]({href})"
                self.out.append(segment)
            self._recount_nl()

    def handle_data(self, data):
        if self.in_pre:
            self._emit(data)
            return
        text = re.sub(r"\s+", " ", data)
        if self.nl > 0 or not self.out:
            text = text.lstrip()
        self._emit(text)


def html_to_markdown(html_text: str) -> str:
    """Convert an HTML post body to compact Markdown."""
    conv = _Converter()
    conv.feed(html_text or "")
    conv.close()
    if conv.in_pre:  # malformed HTML: close the fence
        conv._emit("\n```")
    text = "".join(conv.out)
    # Tidy whitespace only OUTSIDE code fences, so code indentation and blank lines survive.
    parts = text.split("```")
    for i in range(0, len(parts), 2):
        cleaned = re.sub(r"[ \t]+\n", "\n", parts[i])
        parts[i] = re.sub(r"\n{3,}", "\n\n", cleaned)
    return "```".join(parts).strip()


def truncate_text(text: str, max_chars: int, link: str | None = None) -> str:
    """Cut text to roughly max_chars at a natural boundary and add a truncation marker."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    floor = int(max_chars * 0.8)
    for sep in ("\n\n", "\n", " "):
        idx = cut.rfind(sep)
        if idx >= floor:
            cut = cut[:idx]
            break
    cut = cut.rstrip()
    if cut.count("```") % 2 == 1:  # we cut inside a code block: close it
        cut += "\n```"
    marker = f"[truncated, see {link}]" if link else "[truncated]"
    return f"{cut}\n\n{marker}"
