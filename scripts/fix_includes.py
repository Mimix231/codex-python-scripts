#!/usr/bin/env python3
"""
fix_includes.py

Normalize include/import blocks across one or many files.

- preview is the default mode
- supports C-like #include blocks and Python import blocks
- can add, remove, dedupe, and sort entries
- can keep system includes before local includes for C-like files
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_MODE = "preview"
DEFAULT_ENCODING = "utf-8"
PY_ENCODING_RE = re.compile(r"^#.*coding[:=]\s*[-\w.]+")


@dataclass(slots=True)
class FilePlan:
    path: Path
    original_text: str
    updated_text: str
    style: str
    added: int
    removed: int
    final_entries: int


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add, remove, dedupe, and sort include/import blocks with preview-first safety."
    )

    target_group = parser.add_argument_group("targets")
    target_group.add_argument("--files", nargs="+", default=[], help="Explicit file paths.")
    target_group.add_argument(
        "--bulk",
        action="append",
        default=[],
        help="Bulk file list like '[a.c,b.c]' or comma-separated paths.",
    )
    target_group.add_argument(
        "--glob",
        action="append",
        default=[],
        help="Glob pattern expanded from the current working directory.",
    )
    target_group.add_argument(
        "--file-list",
        action="append",
        default=[],
        help="Text file containing one path per line.",
    )

    edit_group = parser.add_argument_group("edits")
    edit_group.add_argument(
        "--style",
        choices=["auto", "c", "python"],
        default="auto",
        help="Block style to edit. Default: auto",
    )
    edit_group.add_argument("--add", action="append", default=[], help="Entry to add. May be repeated.")
    edit_group.add_argument("--remove", action="append", default=[], help="Entry or substring to remove.")
    edit_group.add_argument(
        "--keep-order",
        action="store_true",
        help="Keep the resulting entry order instead of sorting.",
    )
    edit_group.add_argument(
        "--blank-line-between-groups",
        action="store_true",
        help="For C-like files, insert a blank line between system and local includes.",
    )
    edit_group.add_argument(
        "--local-first",
        action="store_true",
        help="For C-like files, place local includes before system includes.",
    )
    edit_group.add_argument(
        "--ignore-missing-removal",
        action="store_true",
        help="Do not fail if a requested removal is not present.",
    )

    behavior_group = parser.add_argument_group("behavior")
    behavior_group.add_argument(
        "--mode",
        choices=["preview", "apply"],
        default=DEFAULT_MODE,
        help="Preview diffs or apply changes.",
    )
    behavior_group.add_argument("--backup", action="store_true", help="Create .bak files in apply mode.")
    behavior_group.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"Text encoding. Default: {DEFAULT_ENCODING}",
    )
    behavior_group.add_argument(
        "--context",
        type=int,
        default=3,
        help="Unified diff context lines.",
    )
    behavior_group.add_argument(
        "--fail-on-noop",
        action="store_true",
        help="Exit with an error if no changes would be made.",
    )
    behavior_group.add_argument(
        "--expect-files",
        type=int,
        help="Expected number of changed files.",
    )
    behavior_group.add_argument(
        "--json-report",
        action="store_true",
        help="Emit a JSON summary after preview/apply output.",
    )

    return parser.parse_args(argv)


def parse_bulk_value(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parts = [item.strip() for item in inner.split(",")]
            return [strip_quotes(item) for item in parts if item.strip()]
        if not isinstance(parsed, list):
            raise SystemExit(f"bulk value must decode to a list: {raw!r}")
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in text.split(",") if item.strip()]


def strip_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def read_text(path: Path, encoding: str) -> str:
    try:
        return path.read_text(encoding=encoding)
    except FileNotFoundError:
        raise SystemExit(f"file not found: {path}")


def write_text(path: Path, text: str, encoding: str) -> None:
    with path.open("w", encoding=encoding, newline="") as handle:
        handle.write(text)


def collect_target_paths(args: argparse.Namespace) -> list[Path]:
    ordered: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path in seen:
            return
        seen.add(path)
        ordered.append(path)

    for raw in args.files:
        add(Path(raw))

    for bulk in args.bulk:
        for item in parse_bulk_value(bulk):
            add(Path(item))

    for list_file in args.file_list:
        for line in read_text(Path(list_file), args.encoding).splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                add(Path(stripped))

    for pattern in args.glob:
        matches = sorted(Path.cwd().glob(pattern))
        if not matches:
            raise SystemExit(f"glob matched no paths: {pattern!r}")
        for match in matches:
            if match.is_dir():
                raise SystemExit(f"glob matched a directory, not a file: {match}")
            add(match)

    if not ordered:
        raise SystemExit("no target files specified")

    return ordered


def detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def detect_style(path: Path, args: argparse.Namespace) -> str:
    if args.style != "auto":
        return args.style
    suffix = path.suffix.lower()
    if suffix in {".py", ".pyw"}:
        return "python"
    return "c"


def is_c_include_line(stripped: str) -> bool:
    return stripped.startswith("#include ") or stripped == "#include"


def is_python_import_line(stripped: str) -> bool:
    return stripped.startswith("import ") or stripped.startswith("from ")


def normalize_c_entry(entry: str) -> str:
    stripped = entry.strip()
    if stripped.startswith("#include"):
        return stripped
    if stripped.startswith("<") or stripped.startswith('"'):
        return f"#include {stripped}"
    return f"#include \"{stripped}\""


def normalize_python_entry(entry: str) -> str:
    stripped = entry.strip()
    if stripped.startswith("import ") or stripped.startswith("from "):
        return stripped
    return f"import {stripped}"


def split_lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def skip_c_prologue(lines: list[str]) -> int:
    i = 0
    if lines and lines[0].startswith("#!"):
        i += 1
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "":
            i += 1
            continue
        if stripped == "#pragma once":
            i += 1
            continue
        if stripped.startswith("//"):
            i += 1
            continue
        if stripped.startswith("/*"):
            i += 1
            while i < len(lines) and "*/" not in lines[i - 1]:
                i += 1
            continue
        break
    return i


def skip_python_prologue(lines: list[str]) -> int:
    i = 0
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    if i < len(lines) and PY_ENCODING_RE.match(lines[i].strip()):
        i += 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i < len(lines):
        stripped = lines[i].lstrip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            quote = stripped[:3]
            i += 1
            while i < len(lines) and quote not in lines[i]:
                i += 1
            if i < len(lines):
                i += 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    return i


def find_block(lines: list[str], start_index: int, checker) -> tuple[int, int]:
    start = start_index
    while start < len(lines) and lines[start].strip() == "":
        start += 1

    end = start
    found_entry = False
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped == "":
            if found_entry:
                end += 1
                continue
            break
        if checker(stripped):
            found_entry = True
            end += 1
            continue
        break

    if not found_entry:
        return start_index, start_index
    return start, end


def filter_removed(entries: list[str], remove_needles: list[str], ignore_missing: bool) -> tuple[list[str], int]:
    if not remove_needles:
        return entries[:], 0

    result: list[str] = []
    removed = 0
    unmatched = remove_needles[:]
    for entry in entries:
        matched = False
        for needle in remove_needles:
            if needle in entry:
                matched = True
                removed += 1
                if needle in unmatched:
                    unmatched.remove(needle)
                break
        if not matched:
            result.append(entry)

    if unmatched and not ignore_missing:
        raise SystemExit(f"remove target(s) not found: {', '.join(unmatched)}")
    return result, removed


def dedupe_preserve_order(entries: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        result.append(entry)
    return result


def format_c_entries(entries: list[str], newline: str, args: argparse.Namespace) -> list[str]:
    if not args.keep_order:
        system_entries = [entry for entry in entries if "<" in entry and ">" in entry]
        local_entries = [entry for entry in entries if entry not in system_entries]
        system_entries.sort()
        local_entries.sort()
        if args.local_first:
            groups = [local_entries, system_entries]
        else:
            groups = [system_entries, local_entries]
        ordered: list[str] = []
        for group in groups:
            if not group:
                continue
            if ordered and args.blank_line_between_groups:
                ordered.append("")
            ordered.extend(group)
        entries = ordered
    return [entry + newline if entry else newline for entry in entries]


def format_python_entries(entries: list[str], newline: str, args: argparse.Namespace) -> list[str]:
    if not args.keep_order:
        entries = sorted(entries)
    return [entry + newline for entry in entries]


def build_c_plan(path: Path, original_text: str, args: argparse.Namespace) -> FilePlan | None:
    newline = detect_newline(original_text)
    lines = split_lines(original_text)
    prologue_end = skip_c_prologue(lines)
    block_start, block_end = find_block(lines, prologue_end, is_c_include_line)
    existing = [line.strip() for line in lines[block_start:block_end] if is_c_include_line(line.strip())]

    filtered, removed = filter_removed(existing, args.remove, args.ignore_missing_removal)
    updated_entries = dedupe_preserve_order(filtered + [normalize_c_entry(item) for item in args.add])
    added = len(updated_entries) - len(filtered)
    block_lines = format_c_entries(updated_entries, newline, args)

    new_lines = lines[:block_start] + block_lines + lines[block_end:]
    updated_text = "".join(new_lines)
    if updated_text == original_text:
        return None

    return FilePlan(
        path=path,
        original_text=original_text,
        updated_text=updated_text,
        style="c",
        added=max(0, added),
        removed=removed,
        final_entries=len(updated_entries),
    )


def build_python_plan(path: Path, original_text: str, args: argparse.Namespace) -> FilePlan | None:
    newline = detect_newline(original_text)
    lines = split_lines(original_text)
    prologue_end = skip_python_prologue(lines)
    block_start, block_end = find_block(lines, prologue_end, is_python_import_line)
    existing = [line.strip() for line in lines[block_start:block_end] if is_python_import_line(line.strip())]

    filtered, removed = filter_removed(existing, args.remove, args.ignore_missing_removal)
    updated_entries = dedupe_preserve_order(filtered + [normalize_python_entry(item) for item in args.add])
    added = len(updated_entries) - len(filtered)
    block_lines = format_python_entries(updated_entries, newline, args)

    new_lines = lines[:block_start] + block_lines + lines[block_end:]
    updated_text = "".join(new_lines)
    if updated_text == original_text:
        return None

    return FilePlan(
        path=path,
        original_text=original_text,
        updated_text=updated_text,
        style="python",
        added=max(0, added),
        removed=removed,
        final_entries=len(updated_entries),
    )


def build_file_plan(path: Path, args: argparse.Namespace) -> FilePlan | None:
    original_text = read_text(path, args.encoding)
    style = detect_style(path, args)
    if style == "c":
        return build_c_plan(path, original_text, args)
    if style == "python":
        return build_python_plan(path, original_text, args)
    raise SystemExit(f"unsupported style: {style}")


def validate_expectations(plans: list[FilePlan], args: argparse.Namespace) -> None:
    if args.fail_on_noop and not plans:
        raise SystemExit("no changes would be made")
    if args.expect_files is not None and len(plans) != args.expect_files:
        raise SystemExit(f"changed files {len(plans)}, expected {args.expect_files}")


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


def print_preview(plans: list[FilePlan], args: argparse.Namespace) -> None:
    if not plans:
        print("No changes.")
        return

    for index, plan in enumerate(plans):
        if index:
            print("\n" + ("=" * 80) + "\n")
        print(
            f"# {plan.path} | style={plan.style} | added={plan.added} | removed={plan.removed} | final={plan.final_entries}"
        )
        print(unified_diff(plan.path, plan.original_text, plan.updated_text, args.context))


def apply_changes(plans: list[FilePlan], args: argparse.Namespace) -> None:
    for plan in plans:
        if args.backup:
            shutil.copyfile(plan.path, Path(str(plan.path) + ".bak"))
        write_text(plan.path, plan.updated_text, args.encoding)


def emit_json_report(plans: list[FilePlan]) -> None:
    payload = {
        "changed_files": len(plans),
        "files": [
            {
                "path": str(plan.path),
                "style": plan.style,
                "added": plan.added,
                "removed": plan.removed,
                "final_entries": plan.final_entries,
            }
            for plan in plans
        ],
    }
    print(json.dumps(payload, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    targets = collect_target_paths(args)

    plans: list[FilePlan] = []
    for path in targets:
        plan = build_file_plan(path, args)
        if plan is not None:
            plans.append(plan)

    validate_expectations(plans, args)

    if args.mode == "preview":
        print_preview(plans, args)
    else:
        apply_changes(plans, args)
        print(f"Applied changes to {len(plans)} file(s).")

    if args.json_report:
        emit_json_report(plans)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
