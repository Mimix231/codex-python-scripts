#!/usr/bin/env python3
"""
replace_code.py

Deterministic bulk search-and-replace helper.

- preview is the default mode
- supports literal and regex replacements
- optional first-match or all-match behavior
- can fail on no-op and enforce expected replacement counts
- emits unified diffs and optional JSON reports
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


@dataclass(slots=True)
class FilePlan:
    path: Path
    original_text: str
    updated_text: str
    replacements: int
    mode: str


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace code across one or many files with preview-first safety."
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

    find_group = parser.add_argument_group("find")
    find_group.add_argument("--find", help="Literal text to replace.")
    find_group.add_argument("--find-file", help="Read literal search text from a file.")
    find_group.add_argument("--regex", help="Regex pattern to replace.")
    find_group.add_argument(
        "--word-boundary",
        action="store_true",
        help="Wrap literal search with identifier-style boundaries.",
    )
    find_group.add_argument(
        "--ignore-case",
        action="store_true",
        help="Case-insensitive matching.",
    )
    find_group.add_argument(
        "--multiline",
        action="store_true",
        help="Enable regex multiline mode.",
    )
    find_group.add_argument(
        "--dotall",
        action="store_true",
        help="Enable regex dotall mode.",
    )

    replace_group = parser.add_argument_group("replace")
    replace_group.add_argument("--replace", default="", help="Replacement text.")
    replace_group.add_argument("--replace-file", help="Read replacement text from a file.")
    replace_group.add_argument(
        "--all-matches",
        action="store_true",
        help="Replace every match in each file instead of only the first.",
    )
    replace_group.add_argument(
        "--ignore-missing",
        action="store_true",
        help="Do not fail when a target file has no match.",
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
        "--expect-total",
        type=int,
        help="Expected total number of replacements across all files.",
    )
    behavior_group.add_argument(
        "--expect-per-file",
        type=int,
        help="Expected replacements per changed file.",
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


def load_search_text(args: argparse.Namespace) -> tuple[str, str]:
    provided = sum(
        1 for flag in [args.find is not None, args.find_file is not None, args.regex is not None] if flag
    )
    if provided != 1:
        raise SystemExit("provide exactly one of --find, --find-file, or --regex")

    if args.find is not None:
        return args.find, "literal"
    if args.find_file is not None:
        return read_text(Path(args.find_file), args.encoding), "literal"
    return args.regex, "regex"


def load_replace_text(args: argparse.Namespace) -> str:
    if args.replace_file is not None:
        if args.replace != "":
            raise SystemExit("use only one of --replace or --replace-file")
        return read_text(Path(args.replace_file), args.encoding)
    return args.replace


def build_regex_from_literal(literal: str, args: argparse.Namespace) -> re.Pattern[str]:
    if literal == "":
        raise SystemExit("literal search text must not be empty")

    pattern = re.escape(literal)
    if args.word_boundary:
        pattern = rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])"

    flags = 0
    if args.ignore_case:
        flags |= re.IGNORECASE
    if args.multiline:
        flags |= re.MULTILINE
    if args.dotall:
        flags |= re.DOTALL
    return re.compile(pattern, flags)


def build_regex_from_pattern(pattern: str, args: argparse.Namespace) -> re.Pattern[str]:
    flags = 0
    if args.ignore_case:
        flags |= re.IGNORECASE
    if args.multiline:
        flags |= re.MULTILINE
    if args.dotall:
        flags |= re.DOTALL
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise SystemExit(f"invalid regex {pattern!r}: {exc}") from exc


def build_file_plan(
    path: Path,
    regex: re.Pattern[str],
    replacement: str,
    args: argparse.Namespace,
    mode_name: str,
) -> FilePlan | None:
    original_text = read_text(path, args.encoding)
    count = 0 if args.all_matches else 1
    updated_text, replacements = regex.subn(replacement, original_text, count=count)

    if replacements == 0:
        if args.ignore_missing:
            return None
        raise SystemExit(f"no match found in {path}")

    if updated_text == original_text:
        return None

    return FilePlan(
        path=path,
        original_text=original_text,
        updated_text=updated_text,
        replacements=replacements,
        mode=mode_name,
    )


def validate_expectations(plans: list[FilePlan], args: argparse.Namespace) -> None:
    if args.fail_on_noop and not plans:
        raise SystemExit("no changes would be made")

    if args.expect_per_file is not None:
        for plan in plans:
            if plan.replacements != args.expect_per_file:
                raise SystemExit(
                    f"{plan.path} has {plan.replacements} replacements, expected {args.expect_per_file}"
                )

    if args.expect_total is not None:
        total = sum(plan.replacements for plan in plans)
        if total != args.expect_total:
            raise SystemExit(f"total replacements {total}, expected {args.expect_total}")


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
            f"# {plan.path} | mode={plan.mode} | replacements={plan.replacements} | "
            f"bytes={len(plan.updated_text) - len(plan.original_text)}"
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
        "total_replacements": sum(plan.replacements for plan in plans),
        "files": [
            {
                "path": str(plan.path),
                "mode": plan.mode,
                "replacements": plan.replacements,
                "bytes_delta": len(plan.updated_text) - len(plan.original_text),
            }
            for plan in plans
        ],
    }
    print(json.dumps(payload, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    targets = collect_target_paths(args)
    search_text, search_mode = load_search_text(args)
    replacement = load_replace_text(args)

    regex = (
        build_regex_from_literal(search_text, args)
        if search_mode == "literal"
        else build_regex_from_pattern(search_text, args)
    )

    plans: list[FilePlan] = []
    for path in targets:
        plan = build_file_plan(path, regex, replacement, args, search_mode)
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
