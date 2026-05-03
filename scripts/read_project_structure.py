#!/usr/bin/env python3
"""
read_project_structure.py

Summarize the workspace structure, manifests, and a bounded file tree so Abyss
can quickly understand the project shape.
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
MANIFESTS = ["package.json", "pyproject.toml", "Cargo.toml", "requirements.txt", "README.md"]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read a bounded workspace structure summary.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root.")
    parser.add_argument("--max-depth", type=int, default=3, help="Tree depth cap. Default: 3")
    parser.add_argument("--max-entries", type=int, default=200, help="Entry cap. Default: 200")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def infer_stacks(root: Path) -> list[str]:
    stacks = []
    if (root / "package.json").exists():
        stacks.append("node")
    if (root / "Cargo.toml").exists():
        stacks.append("rust")
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        stacks.append("python")
    return stacks or ["unknown"]


def collect_entries(root: Path, max_depth: int, max_entries: int) -> tuple[list[dict[str, object]], bool]:
    entries: list[dict[str, object]] = []
    truncated = False
    for current_root, dir_names, file_names in os.walk(root):
        current = Path(current_root)
        rel_parts = current.relative_to(root).parts
        depth = len(rel_parts)
        dir_names[:] = [name for name in sorted(dir_names) if name not in IGNORED_DIRS]
        file_names = sorted(file_names)
        if depth >= max_depth:
            dir_names[:] = []
        if current != root:
            entries.append(
                {
                    "path": current.relative_to(root).as_posix(),
                    "name": current.name,
                    "isDir": True,
                    "depth": depth,
                }
            )
            if len(entries) >= max_entries:
                truncated = True
                break
        for file_name in file_names:
            file_path = current / file_name
            entries.append(
                {
                    "path": file_path.relative_to(root).as_posix(),
                    "name": file_name,
                    "isDir": False,
                    "depth": depth + (0 if current == root else 1),
                }
            )
            if len(entries) >= max_entries:
                truncated = True
                break
        if truncated:
            break
    return entries, truncated


def render_tree(entries: list[dict[str, object]]) -> str:
    lines = []
    for entry in entries:
        indent = "  " * int(entry["depth"])
        marker = "📁" if entry["isDir"] else "📄"
        lines.append(f"{indent}{marker} {entry['path']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    manifests = [name for name in MANIFESTS if (root / name).exists()]
    stacks = infer_stacks(root)
    entries, truncated = collect_entries(root, max(1, args.max_depth), max(1, args.max_entries))

    print(f"Project structure loaded for {root.name}.")
    print(f"Stacks: {', '.join(stacks)}")
    if manifests:
        print(f"Manifests: {', '.join(manifests)}")

    if args.json_report:
        print(
            json.dumps(
                {
                    "root": root.as_posix(),
                    "workspaceName": root.name,
                    "manifests": manifests,
                    "stacks": stacks,
                    "entries": entries,
                    "count": len(entries),
                    "truncated": truncated,
                    "tree": render_tree(entries),
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
