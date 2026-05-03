#!/usr/bin/env python3
"""
get_changed_files.py

Return changed files from the current workspace's git state.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List changed files from git status.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root.")
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        help="Include untracked files. Default: false",
    )
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    command = ["git", "-C", str(root), "status", "--porcelain"]
    if args.include_untracked:
        command.append("--untracked-files=all")

    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )

    if completed.returncode != 0:
        print("No git status available for this workspace.")
        if args.json_report:
            print(
                json.dumps(
                    {
                        "root": root.as_posix(),
                        "count": 0,
                        "files": [],
                        "available": False,
                        "stderr": completed.stderr,
                    },
                    indent=2,
                )
            )
        return 0

    files = []
    for raw_line in completed.stdout.splitlines():
        if len(raw_line) < 4:
            continue
        status = raw_line[:2]
        path = raw_line[3:].strip()
        if not path:
            continue
        files.append({"path": path.replace("\\", "/"), "status": status})

    print(f"Detected {len(files)} changed file(s).")
    for item in files:
        print(f"- [{item['status']}] {item['path']}")

    if args.json_report:
        print(
            json.dumps(
                {
                    "root": root.as_posix(),
                    "count": len(files),
                    "files": files,
                    "available": True,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
