#!/usr/bin/env python3
"""
bulk_rename_paths.py

Rename files and directories in bulk, optionally updating textual references in
other files.

Features:
- rename pairs from CLI or JSON map file
- preview-first path move plan
- optional reference updates across explicit files, file lists, or globs
- optional missing-source tolerance and destination overwrite control
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_MODE = "preview"
DEFAULT_ENCODING = "utf-8"


@dataclass(slots=True)
class RenamePair:
    source: Path
    destination: Path


@dataclass(slots=True)
class FilePlan:
    path: Path
    original_text: str
    updated_text: str
    replacements: int


@dataclass(slots=True)
class PathPlan:
    source: Path
    destination: Path
    kind: str


@dataclass(slots=True)
class RenamePlan:
    path_moves: list[PathPlan]
    file_updates: list[FilePlan]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rename files/directories in bulk and optionally rewrite text references."
    )
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        help="Rename pair in OLD=NEW form. May be repeated.",
    )
    parser.add_argument(
        "--map-file",
        help="JSON object mapping old paths to new paths, or a list of {from,to} objects.",
    )
    parser.add_argument(
        "--update-refs",
        action="store_true",
        help="Update textual path references in target files.",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        default=[],
        help="Explicit files for reference updates.",
    )
    parser.add_argument(
        "--glob",
        action="append",
        default=[],
        help="Glob pattern for reference update targets.",
    )
    parser.add_argument(
        "--file-list",
        action="append",
        default=[],
        help="Text file containing one update-target path per line.",
    )
    parser.add_argument(
        "--exclude-glob",
        action="append",
        default=[],
        help="Glob pattern to exclude from reference update targets.",
    )
    parser.add_argument(
        "--ignore-missing-source",
        action="store_true",
        help="Skip rename pairs whose source path does not exist.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow destination paths to be overwritten during apply.",
    )
    parser.add_argument(
        "--mode",
        choices=["preview", "apply"],
        default=DEFAULT_MODE,
        help="Preview diffs or apply changes.",
    )
    parser.add_argument("--backup", action="store_true", help="Create .bak files for changed reference files in apply mode.")
    parser.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"Text encoding for reference file updates. Default: {DEFAULT_ENCODING}",
    )
    parser.add_argument("--context", type=int, default=3, help="Unified diff context lines.")
    parser.add_argument("--fail-on-noop", action="store_true", help="Exit with an error if no rename/update would occur.")
    parser.add_argument("--json-report", action="store_true", help="Emit a JSON summary after preview/apply output.")
    return parser.parse_args(argv)


def read_text(path: Path, encoding: str) -> str:
    try:
        return path.read_text(encoding=encoding)
    except FileNotFoundError:
        raise SystemExit(f"file not found: {path}")


def write_text(path: Path, text: str, encoding: str) -> None:
    with path.open("w", encoding=encoding, newline="") as handle:
        handle.write(text)


def parse_pair(raw: str) -> RenamePair:
    if "=" not in raw:
        raise SystemExit(f"--pair expects OLD=NEW: {raw!r}")
    old, new = raw.split("=", 1)
    old = old.strip()
    new = new.strip()
    if not old or not new:
        raise SystemExit(f"--pair expects non-empty OLD and NEW: {raw!r}")
    return RenamePair(Path(old), Path(new))


def load_pairs(args: argparse.Namespace) -> list[RenamePair]:
    pairs: list[RenamePair] = [parse_pair(raw) for raw in args.pair]
    if args.map_file:
        payload = json.loads(read_text(Path(args.map_file), args.encoding))
        if isinstance(payload, dict):
            for source, destination in payload.items():
                pairs.append(RenamePair(Path(str(source)), Path(str(destination))))
        elif isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict) or "from" not in item or "to" not in item:
                    raise SystemExit("map file list items must be objects with 'from' and 'to'")
                pairs.append(RenamePair(Path(str(item["from"])), Path(str(item["to"]))))
        else:
            raise SystemExit("map file must be a JSON object or a list of {from,to} objects")
    if not pairs:
        raise SystemExit("no rename pairs specified")
    return pairs


def collect_reference_targets(args: argparse.Namespace) -> list[Path]:
    ordered: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path in seen:
            return
        seen.add(path)
        ordered.append(path)

    for raw in args.files:
        add(Path(raw))

    for file_list in args.file_list:
        for line in read_text(Path(file_list), args.encoding).splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                add(Path(stripped))

    for pattern in args.glob:
        matches = sorted(Path.cwd().glob(pattern))
        if not matches:
            raise SystemExit(f"glob matched no paths: {pattern!r}")
        for match in matches:
            if match.is_dir():
                continue
            add(match)

    if args.exclude_glob:
        filtered: list[Path] = []
        for path in ordered:
            normalized = str(path).replace("\\", "/")
            if any(fnmatch.fnmatch(normalized, pattern) for pattern in args.exclude_glob):
                continue
            filtered.append(path)
        ordered = filtered

    return ordered


def unified_diff(path: Path, before: str, after: str, context: int) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
        )
    )


def replace_literal_all(text: str, old: str, new: str) -> tuple[str, int]:
    if old == new:
        return text, 0
    count = text.count(old)
    if count == 0:
        return text, 0
    return text.replace(old, new), count


def build_path_moves(pairs: list[RenamePair], args: argparse.Namespace) -> list[PathPlan]:
    plans: list[PathPlan] = []
    for pair in pairs:
        if not pair.source.exists():
            if args.ignore_missing_source:
                continue
            raise SystemExit(f"source path does not exist: {pair.source}")
        if pair.destination.exists() and not args.force:
            raise SystemExit(f"destination already exists: {pair.destination}")
        kind = "directory" if pair.source.is_dir() else "file"
        plans.append(PathPlan(pair.source, pair.destination, kind))
    return plans


def build_reference_updates(pairs: list[RenamePair], targets: list[Path], args: argparse.Namespace) -> list[FilePlan]:
    plans: list[FilePlan] = []
    if not args.update_refs:
        return plans
    if not targets:
        raise SystemExit("--update-refs requires --files, --glob, or --file-list targets")

    for path in targets:
        original = read_text(path, args.encoding)
        updated = original
        replacements = 0
        for pair in pairs:
            source_forward = str(pair.source).replace("\\", "/")
            destination_forward = str(pair.destination).replace("\\", "/")
            updated, count_forward = replace_literal_all(updated, source_forward, destination_forward)
            replacements += count_forward
            source_native = str(pair.source)
            destination_native = str(pair.destination)
            if source_native != source_forward:
                updated, count_native = replace_literal_all(updated, source_native, destination_native)
                replacements += count_native
        if updated != original:
            plans.append(FilePlan(path, original, updated, replacements))
    return plans


def build_plan(args: argparse.Namespace) -> RenamePlan:
    pairs = load_pairs(args)
    path_moves = build_path_moves(pairs, args)
    ref_targets = collect_reference_targets(args)
    file_updates = build_reference_updates(pairs, ref_targets, args)
    return RenamePlan(path_moves, file_updates)


def print_preview(plan: RenamePlan, args: argparse.Namespace) -> None:
    if not plan.path_moves and not plan.file_updates:
        print("No changes.")
        return

    if plan.path_moves:
        print("# Path moves")
        for move in plan.path_moves:
            print(f"- [{move.kind}] {move.source} -> {move.destination}")

    if plan.file_updates:
        print("\n# Reference updates")
        for index, file_plan in enumerate(plan.file_updates):
            if index:
                print("\n" + ("=" * 80) + "\n")
            print(
                f"# {file_plan.path} | replacements={file_plan.replacements} | "
                f"bytes={len(file_plan.updated_text) - len(file_plan.original_text)}"
            )
            print(unified_diff(file_plan.path, file_plan.original_text, file_plan.updated_text, args.context))


def apply_plan(plan: RenamePlan, args: argparse.Namespace) -> None:
    for file_plan in plan.file_updates:
        if args.backup:
            shutil.copyfile(file_plan.path, Path(str(file_plan.path) + ".bak"))
        write_text(file_plan.path, file_plan.updated_text, args.encoding)

    # Rename deeper paths first so nested directory moves behave predictably.
    sorted_moves = sorted(plan.path_moves, key=lambda item: len(str(item.source)), reverse=True)
    for move in sorted_moves:
        if move.destination.exists() and args.force:
            if move.destination.is_dir():
                shutil.rmtree(move.destination)
            else:
                move.destination.unlink()
        move.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move.source), str(move.destination))


def emit_json_report(plan: RenamePlan) -> None:
    payload = {
        "path_moves": [
            {
                "source": str(move.source),
                "destination": str(move.destination),
                "kind": move.kind,
            }
            for move in plan.path_moves
        ],
        "reference_updates": [
            {
                "path": str(file_plan.path),
                "replacements": file_plan.replacements,
            }
            for file_plan in plan.file_updates
        ],
    }
    print(json.dumps(payload, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    plan = build_plan(args)

    if args.fail_on_noop and not plan.path_moves and not plan.file_updates:
        raise SystemExit("no rename or reference updates would be made")

    if args.mode == "preview":
        print_preview(plan, args)
    else:
        if not plan.path_moves and not plan.file_updates:
            print("No changes.")
        else:
            apply_plan(plan, args)
            print(
                f"Applied {len(plan.path_moves)} path move(s) and {len(plan.file_updates)} reference update file(s)."
            )

    if args.json_report:
        emit_json_report(plan)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
