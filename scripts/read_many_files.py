#!/usr/bin/env python3
"""
read_many_files.py

Read many files at once with deterministic limits so Abyss can fetch the
relevant local context in one tool call.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Sequence


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read many files with deterministic truncation limits."
    )
    parser.add_argument("--files", nargs="*", default=[], help="Explicit file paths.")
    parser.add_argument("--glob", action="append", default=[], help="Glob pattern from the current directory.")
    parser.add_argument("--file-list", action="append", default=[], help="Text file containing one path per line.")
    parser.add_argument(
        "--max-bytes-per-file",
        type=int,
        default=12000,
        help="Maximum bytes to read per file. Default: 12000",
    )
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def gather_paths(args: argparse.Namespace) -> list[Path]:
    ordered: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path
        if resolved in seen:
            return
        seen.add(resolved)
        ordered.append(resolved)

    for raw in args.files:
        add(Path(raw))
    for pattern in args.glob:
        for match in sorted(glob.glob(pattern, recursive=True)):
            add(Path(match))
    for file_list in args.file_list:
        for line in Path(file_list).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                add(Path(stripped))

    return ordered


def read_file(path: Path, max_bytes: int) -> dict[str, object]:
    raw = path.read_bytes()
    truncated = len(raw) > max_bytes
    content = raw[:max_bytes].decode("utf-8", errors="replace")
    return {
        "path": path.as_posix(),
        "exists": path.exists(),
        "truncated": truncated,
        "bytes": len(raw),
        "content": content,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    paths = gather_paths(args)
    if not paths:
        raise SystemExit("no files provided")

    files: list[dict[str, object]] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            files.append({
                "path": path.as_posix(),
                "exists": False,
                "truncated": False,
                "bytes": 0,
                "content": "",
            })
            continue
        files.append(read_file(path, max(256, args.max_bytes_per_file)))

    print(f"Read {len(files)} file(s).")
    for item in files:
        print(f"- {item['path']}")

    if args.json_report:
        print(json.dumps({"files": files, "count": len(files)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
