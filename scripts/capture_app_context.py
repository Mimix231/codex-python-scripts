#!/usr/bin/env python3
"""
capture_app_context.py

Fetch HTML and tail runtime logs from the active app preview so Abyss can see
what the running app is actually doing.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Sequence

RUNTIME_DIR = Path(".glitch") / "runtime"
METADATA_PATH = RUNTIME_DIR / "app_preview.json"


class PreviewHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capture_title = False
        self._ignore_depth = 0
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self._capture_title = True
        if tag in {"script", "style", "noscript"}:
            self._ignore_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._capture_title = False
        if tag in {"script", "style", "noscript"} and self._ignore_depth > 0:
            self._ignore_depth -= 1
        if tag in {"p", "div", "section", "article", "li", "br"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignore_depth > 0:
            return
        if self._capture_title:
            self.title_parts.append(data)
        self.text_parts.append(data)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture HTML and log context from the running preview."
    )
    parser.add_argument("--workspace-root", default=".", help="Workspace root. Default: current directory")
    parser.add_argument("--url", help="Explicit URL to inspect. If omitted, the active preview metadata is used.")
    parser.add_argument("--max-chars", type=int, default=12000, help="Maximum HTML text characters. Default: 12000")
    parser.add_argument("--log-lines", type=int, default=120, help="Maximum log lines to include. Default: 120")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def read_metadata(root: Path) -> dict[str, object] | None:
    path = root / METADATA_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def fetch_html(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "GlitchAbyss/1.0 (+local-tool)"})
    with urllib.request.urlopen(request, timeout=10) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read().decode(charset, errors="replace")
    parser = PreviewHtmlParser()
    parser.feed(body)
    return {
        "title": normalize_space("".join(parser.title_parts)) or url,
        "content": normalize_space(" ".join(parser.text_parts)),
    }


def tail_log(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max(1, max_lines):]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or [])
    root = Path(args.workspace_root).resolve()
    metadata = read_metadata(root) or {}
    url = args.url or metadata.get("url")
    log_path = Path(metadata.get("logPath", root / ".glitch" / "runtime" / "app_preview.log"))

    report: dict[str, object] = {
        "url": url,
        "title": None,
        "content": "",
        "logTail": tail_log(log_path, args.log_lines),
        "errors": [],
    }

    if url:
        try:
            page = fetch_html(str(url))
            report["title"] = page["title"]
            report["content"] = str(page["content"])[: max(500, args.max_chars)]
        except Exception as error:  # pragma: no cover - network/runtime path
            report["errors"] = [f"Failed to fetch {url}: {error}"]
    else:
        report["errors"] = ["No preview URL is available."]

    print(f"Captured app context from: {url or 'no url'}")
    if report["title"]:
        print(f"Title: {report['title']}")
    if report["logTail"]:
        print("Recent log tail:")
        for line in report["logTail"][-10:]:
            print(line)

    if args.json_report:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
