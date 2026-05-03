#!/usr/bin/env python3
"""
read_file_range.py

Read a bounded line range from a single file so Abyss can inspect files without
always loading the full content.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read a bounded line range from a file."
    )
    parser.add_argument("--path", required=True, help="Relative or absolute file path.")
    parser.add_argument(
        "--start-line",
        type=int,
        default=1,
        help="1-based start line. Default: 1",
    )
    parser.add_argument(
        "--end-line",
        type=int,
        default=200,
        help="1-based end line. Default: 200",
    )
    parser.add_argument(
        "--json-report",
        action="store_true",
        help="Emit JSON after the text summary.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    path = Path(args.path)
    if not path.exists() or not path.is_file():
        raise SystemExit(f"file does not exist: {path}")

    start_line = max(1, args.start_line)
    end_line = max(start_line, args.end_line)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    excerpt = lines[start_line - 1 : end_line]

    print(
        f"Read lines {start_line}-{min(end_line, len(lines))} from {path.as_posix()}."
    )

    if args.json_report:
        print(
            json.dumps(
                {
                    "path": path.as_posix(),
                    "startLine": start_line,
                    "endLine": min(end_line, len(lines)),
                    "requestedEndLine": end_line,
                    "totalLines": len(lines),
                    "content": "\n".join(excerpt),
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
