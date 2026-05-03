#!/usr/bin/env python3
"""
list_dir.py

List a directory in a structured, bounded way so Abyss can inspect part of the
workspace tree without reading the full workspace snapshot.
"""

from __future__ import annotations

import argparse
import json
import os
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


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List a directory with depth limits.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root.")
    parser.add_argument("--path", default=".", help="Relative path to list.")
    parser.add_argument("--recursive", action="store_true", help="Traverse children recursively.")
    parser.add_argument("--max-depth", type=int, default=2, help="Recursive depth cap. Default: 2")
    parser.add_argument("--max-entries", type=int, default=200, help="Entry cap. Default: 200")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def safe_target(root: Path, raw_path: str) -> Path:
    target = (root / raw_path).resolve() if not Path(raw_path).is_absolute() else Path(raw_path).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as error:
        raise SystemExit(f"path escapes workspace root: {raw_path}") from error
    return target


def collect_entries(target: Path, max_depth: int, recursive: bool, max_entries: int) -> tuple[list[dict[str, object]], bool]:
    entries: list[dict[str, object]] = []
    truncated = False
    base_depth = len(target.parts)

    for current_root, dir_names, file_names in os.walk(target):
        current = Path(current_root)
        depth = len(current.parts) - base_depth
        dir_names[:] = [name for name in sorted(dir_names) if name not in IGNORED_DIRS]
        file_names = sorted(file_names)

        if not recursive and depth > 0:
            dir_names[:] = []
            continue
        if recursive and depth >= max_depth:
            dir_names[:] = []

        if current != target:
            entries.append(
                {
                    "path": current.as_posix(),
                    "name": current.name,
                    "isDir": True,
                    "depth": depth,
                }
            )
            if len(entries) >= max_entries:
                truncated = True
                break

        for name in file_names:
            file_path = current / name
            entries.append(
                {
                    "path": file_path.as_posix(),
                    "name": name,
                    "isDir": False,
                    "depth": depth + 1 if current != target else depth,
                }
            )
            if len(entries) >= max_entries:
                truncated = True
                break
        if truncated:
            break

    if not recursive:
        direct_entries = []
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if child.name in IGNORED_DIRS:
                continue
            direct_entries.append(
                {
                    "path": child.as_posix(),
                    "name": child.name,
                    "isDir": child.is_dir(),
                    "depth": 0,
                }
            )
            if len(direct_entries) >= max_entries:
                truncated = True
                break
        return direct_entries, truncated

    return entries, truncated


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    target = safe_target(root, args.path)
    if not target.exists() or not target.is_dir():
        raise SystemExit(f"directory does not exist: {target}")

    entries, truncated = collect_entries(
        target,
        max_depth=max(0, args.max_depth),
        recursive=args.recursive,
        max_entries=max(1, args.max_entries),
    )

    print(
        f"Listed {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} in {target.relative_to(root).as_posix() if target != root else '.'}."
    )
    if args.json_report:
        print(
            json.dumps(
                {
                    "workspaceRoot": root.as_posix(),
                    "path": target.relative_to(root).as_posix() if target != root else ".",
                    "count": len(entries),
                    "truncated": truncated,
                    "entries": entries,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
