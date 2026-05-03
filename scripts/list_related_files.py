#!/usr/bin/env python3
"""
list_related_files.py

Find files that are probably related to a path, symbol, or request so Abyss can
inspect the right local context before editing.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Sequence

IGNORED_DIRS = {".git", ".next", ".turbo", ".venv", "__pycache__", "build", "dist", "node_modules", "out", "target"}
TEXT_EXTENSIONS = {".c", ".cc", ".cpp", ".css", ".go", ".h", ".hpp", ".html", ".ini", ".java", ".js", ".json", ".jsx", ".lua", ".md", ".mjs", ".py", ".rs", ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml"}


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List files related to a path, symbol, or prompt."
    )
    parser.add_argument("--workspace-root", default=".", help="Workspace root. Default: current directory")
    parser.add_argument("--path", help="Reference path.")
    parser.add_argument("--symbol", help="Reference symbol.")
    parser.add_argument("--prompt", help="Request text.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum results. Default: 20")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def tokenize(text: str) -> list[str]:
    return [token for token in re.findall(r"[A-Za-z0-9_+-]+", (text or "").lower()) if len(token) >= 2]


def score_candidate(path: Path, root: Path, target_path: Path | None, symbol: str | None, prompt_tokens: list[str]) -> tuple[int, list[str]]:
    relative = path.relative_to(root).as_posix().lower()
    score = 0
    reasons: list[str] = []

    if target_path is not None:
        if path.stem == target_path.stem:
            score += 8
            reasons.append("same file stem")
        if path.parent == target_path.parent:
            score += 4
            reasons.append("same directory")
        if target_path.name.lower() in relative:
            score += 3
            reasons.append("mentions target file name")

    content = ""
    if symbol or prompt_tokens:
        try:
            content = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            content = ""

    if symbol:
        token = symbol.lower()
        if token in relative:
            score += 5
            reasons.append("symbol appears in path")
        if token and token in content:
            score += 8
            reasons.append("symbol appears in content")

    matched_tokens = 0
    for token in prompt_tokens:
        if token in relative:
            score += 2
            matched_tokens += 1
        elif token in content:
            score += 1
            matched_tokens += 1
    if matched_tokens:
        reasons.append(f"matched {matched_tokens} prompt token(s)")

    return score, reasons


def collect_candidates(root: Path, target_path: Path | None, symbol: str | None, prompt_tokens: list[str], limit: int) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if name not in IGNORED_DIRS]
        for file_name in file_names:
            path = Path(current_root) / file_name
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            score, reasons = score_candidate(path, root, target_path, symbol, prompt_tokens)
            if score <= 0:
                continue
            results.append({
                "path": path.relative_to(root).as_posix(),
                "score": score,
                "reasons": reasons,
            })
    results.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    return results[: max(1, limit)]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    if not any([args.path, args.symbol, args.prompt]):
        raise SystemExit("provide at least one of --path, --symbol, or --prompt")

    target_path = Path(args.path) if args.path else None
    prompt_tokens = tokenize(args.prompt or "")
    results = collect_candidates(root, target_path, args.symbol, prompt_tokens, args.limit)

    print(f"Related files: {len(results)}")
    for item in results:
        print(f"- {item['path']} ({', '.join(item['reasons'])})")

    if args.json_report:
        import json
        print(json.dumps({"relatedFiles": results, "count": len(results)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
