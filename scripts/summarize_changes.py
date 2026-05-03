#!/usr/bin/env python3
"""
summarize_changes.py

Summarize the most relevant changed files and runnable instructions so Abyss can
close out a coding pass with a clean, structured response.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

IGNORED_DIRS = {".git", ".next", ".turbo", ".venv", "__pycache__", "build", "dist", "node_modules", "out", "target"}
TEXT_EXTENSIONS = {".c", ".cc", ".cpp", ".css", ".go", ".h", ".hpp", ".html", ".ini", ".java", ".js", ".json", ".jsx", ".lua", ".md", ".mjs", ".py", ".rs", ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml"}


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize changed files and likely run instructions."
    )
    parser.add_argument("--workspace-root", default=".", help="Workspace root. Default: current directory")
    parser.add_argument("--files", nargs="*", default=[], help="Explicit files to summarize.")
    parser.add_argument("--prompt", default="", help="Original user prompt for context.")
    parser.add_argument("--since-minutes", type=int, default=180, help="Fallback mtime window when --files is omitted. Default: 180")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def collect_recent_files(root: Path, since_minutes: int) -> list[Path]:
    cutoff = time.time() - max(1, since_minutes) * 60
    files: list[Path] = []
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if name not in IGNORED_DIRS]
        for file_name in file_names:
            path = Path(current_root) / file_name
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    files.append(path)
            except OSError:
                continue
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return files[:20]


def git_changed_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    files = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if path:
            files.append(root / path)
    return files


def infer_run_instructions(root: Path) -> list[str]:
    instructions: list[str] = []
    if (root / "package.json").exists():
        instructions.append("npm run build")
    if (root / "Cargo.toml").exists():
        instructions.append("cargo check")
    if (root / "main.py").exists():
        instructions.append("python main.py")
    return instructions


def summarize_file(path: Path, root: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    preview_lines = [line for line in text.splitlines() if line.strip()][:5]
    return {
        "path": path.relative_to(root).as_posix(),
        "preview": preview_lines,
        "bytes": len(text.encode("utf-8", errors="ignore")),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    explicit = [root / item for item in args.files]
    files = explicit or git_changed_files(root) or collect_recent_files(root, args.since_minutes)
    files = [path for path in files if path.exists() and path.is_file()]
    summaries = [summarize_file(path, root) for path in files[:12]]
    instructions = infer_run_instructions(root)
    report = {
        "prompt": args.prompt,
        "fileCount": len(summaries),
        "files": summaries,
        "runInstructions": instructions,
        "summary": (
            f"Updated {len(summaries)} file(s): " + ", ".join(item["path"] for item in summaries)
            if summaries else "No changed files were detected."
        ),
    }

    print(report["summary"])
    if instructions:
        print("Run instructions:")
        for instruction in instructions:
            print(f"- {instruction}")

    if args.json_report:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
