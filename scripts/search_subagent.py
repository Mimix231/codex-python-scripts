#!/usr/bin/env python3
"""
search_subagent.py

Collect a focused, request-aware search packet for analysis or planning. This
acts as a lightweight search specialist that bundles related files and excerpts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Sequence

IGNORED_DIRS = {
    ".git",
    ".next",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "out",
    "target",
}
TEXT_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".css", ".go", ".h", ".hpp", ".html", ".ini",
    ".java", ".js", ".json", ".jsx", ".lua", ".md", ".mjs", ".py", ".rs",
    ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}
MANIFESTS = ["package.json", "pyproject.toml", "Cargo.toml", "requirements.txt", "README.md"]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a focused search packet for the current request.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root.")
    parser.add_argument("--prompt", required=True, help="Request or search goal.")
    parser.add_argument("--path", help="Optional anchor file path.")
    parser.add_argument("--symbol", help="Optional anchor symbol.")
    parser.add_argument("--limit", type=int, default=8, help="Maximum files to return. Default: 8")
    parser.add_argument(
        "--max-bytes-per-file",
        type=int,
        default=5000,
        help="Per-file byte cap. Default: 5000",
    )
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def tokenize(text: str) -> list[str]:
    return [token for token in re.findall(r"[A-Za-z0-9_+-]+", text.lower()) if len(token) >= 2]


def should_scan(path: Path) -> bool:
    return path.is_file() and (path.suffix.lower() in TEXT_EXTENSIONS or path.name in MANIFESTS)


def score_file(path: Path, root: Path, tokens: list[str], symbol: str | None, anchor_path: str | None) -> tuple[int, str, str]:
    relative = path.relative_to(root).as_posix()
    relative_lower = relative.lower()
    score = 0
    reasons: list[str] = []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, "", ""

    if path.name in MANIFESTS:
        score += 10
        reasons.append("manifest")

    for token in tokens:
        if token in relative_lower:
            score += 4
            reasons.append(f"path:{token}")
        if token in content.lower():
            score += 2
            reasons.append(f"content:{token}")

    if symbol and symbol.lower() in content.lower():
        score += 8
        reasons.append(f"symbol:{symbol}")

    if anchor_path and anchor_path.lower() in relative_lower:
        score += 12
        reasons.append("anchor file")

    return score, ", ".join(dict.fromkeys(reasons)), content[:5000]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    tokens = tokenize(args.prompt)
    candidates = []
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if name not in IGNORED_DIRS]
        for file_name in file_names:
            path = Path(current_root) / file_name
            if not should_scan(path):
                continue
            score, reason, excerpt = score_file(path, root, tokens, args.symbol, args.path)
            if score <= 0:
                continue
            candidates.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "score": score,
                    "reason": reason or "request-relevant",
                    "content": excerpt[: max(500, args.max_bytes_per_file)],
                }
            )
    candidates.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    selected = candidates[: max(1, args.limit)]

    print(f"Search subagent selected {len(selected)} file(s).")
    for item in selected:
        print(f"- {item['path']} ({item['reason']})")

    if args.json_report:
        print(
            json.dumps(
                {
                    "root": root.as_posix(),
                    "prompt": args.prompt,
                    "anchorPath": args.path,
                    "anchorSymbol": args.symbol,
                    "selectedCount": len(selected),
                    "files": selected,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
