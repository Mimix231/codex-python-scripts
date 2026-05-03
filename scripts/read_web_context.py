#!/usr/bin/env python3
"""
read_web_context.py

Fetch a public web page, strip noisy markup, and emit readable context blocks,
headings, and code snippets for Abyss.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.request
from html.parser import HTMLParser
from typing import Sequence

USER_AGENT = "GlitchAbyss/1.0 (+local-tool)"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class ContextHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._ignore_depth = 0
        self._tag_stack: list[str] = []
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._headings: list[str] = []
        self._code_blocks: list[str] = []
        self._current_code: list[str] = []
        self._capture_title = False
        self._capture_heading = False
        self._current_heading: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: D401
        self._tag_stack.append(tag)
        if tag in {"script", "style", "noscript"}:
            self._ignore_depth += 1
        if tag == "title":
            self._capture_title = True
        if tag in {"h1", "h2", "h3", "h4"}:
            self._capture_heading = True
            self._current_heading = []
        if tag in {"pre", "code"}:
            self._current_code = []

    def handle_endtag(self, tag: str) -> None:
        if self._tag_stack:
            self._tag_stack.pop()
        if tag in {"script", "style", "noscript"} and self._ignore_depth > 0:
            self._ignore_depth -= 1
        if tag == "title":
            self._capture_title = False
        if tag in {"h1", "h2", "h3", "h4"}:
            self._capture_heading = False
            heading = normalize_space("".join(self._current_heading))
            if heading:
                self._headings.append(heading)
        if tag in {"pre", "code"}:
            code = normalize_space("".join(self._current_code))
            if code:
                self._code_blocks.append(code)
            self._current_code = []
        if tag in {"p", "div", "section", "article", "li", "br"}:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignore_depth > 0:
            return
        if self._capture_title:
            self._title_parts.append(data)
        if self._capture_heading:
            self._current_heading.append(data)
        if any(tag in {"pre", "code"} for tag in self._tag_stack):
            self._current_code.append(data)
        self._text_parts.append(data)

    @property
    def title(self) -> str:
        return normalize_space("".join(self._title_parts))

    @property
    def content(self) -> str:
        return normalize_space(" ".join(self._text_parts))

    @property
    def headings(self) -> list[str]:
        return self._headings[:20]

    @property
    def code_blocks(self) -> list[str]:
        return self._code_blocks[:8]


def normalize_space(value: str) -> str:
    unescaped = html.unescape(value)
    without_tabs = re.sub(r"[ \t\r\f\v]+", " ", unescaped)
    collapsed = re.sub(r"\n\s*\n+", "\n\n", without_tabs)
    return collapsed.strip()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read a public web page and emit compact text context."
    )
    parser.add_argument("--url", required=True, help="Absolute http or https URL.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout in seconds. Default: 15",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=24000,
        help="Maximum text characters to include in the report. Default: 24000",
    )
    parser.add_argument(
        "--json-report",
        action="store_true",
        help="Emit a JSON summary after the human-readable output.",
    )
    return parser.parse_args(argv)


def fetch_url(url: str, timeout: float) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read().decode(charset, errors="replace")
    return content_type, body


def build_report(url: str, content_type: str, body: str, max_chars: int) -> dict[str, object]:
    if content_type.startswith("text/html"):
        parser = ContextHtmlParser()
        parser.feed(body)
        content = parser.content[:max_chars]
        return {
            "url": url,
            "title": parser.title or url,
            "contentType": content_type,
            "content": content,
            "headings": parser.headings,
            "codeBlocks": parser.code_blocks,
        }

    content = normalize_space(body)[:max_chars]
    return {
        "url": url,
        "title": url,
        "contentType": content_type,
        "content": content,
        "headings": [],
        "codeBlocks": [],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        content_type, body = fetch_url(args.url, args.timeout)
    except Exception as error:  # pragma: no cover - network failure path
        print(f"read failed: {error}", file=sys.stderr)
        return 1

    report = build_report(args.url, content_type, body, max(1000, args.max_chars))
    print(f"Read: {report['title']}")
    print(f"URL: {report['url']}")
    if report["headings"]:
        print("Headings:")
        for heading in report["headings"][:8]:
            print(f"- {heading}")
    if report["content"]:
        print("Context:")
        print(str(report["content"])[: min(len(str(report["content"])), 1200)])

    if args.json_report:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
