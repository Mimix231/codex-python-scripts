#!/usr/bin/env python3
"""
rename_symbol.py

Deterministic repo-wide symbol rename helper.

- preview is the default mode
- supports identifier-boundary, word-boundary, literal, and regex modes
- can apply a single rename or a JSON mapping file
- supports target exclusions and expected rename counts
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
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
class RenameRule:
    source: str
    target: str
    mode: str


@dataclass(slots=True)
class FilePlan:
    path: Path
    original_text: str
    updated_text: str
    renames: int


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rename symbols across one or many files with preview-first safety."
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
    target_group.add_argument(
        "--exclude-glob",
        action="append",
        default=[],
        help="Glob pattern to exclude from the target set.",
    )

    rename_group = parser.add_argument_group("rename")
    rename_group.add_argument("--from-name", help="The source symbol or literal text.")
    rename_group.add_argument("--to-name", help="The destination symbol or literal text.")
    rename_group.add_argument(
        "--map-file",
        help="JSON object mapping old names to new names, or a list of {from,to,mode} objects.",
    )
    rename_group.add_argument(
        "--match-mode",
        choices=["identifier", "word", "literal", "regex"],
        default="identifier",
        help="How --from-name should be matched. Default: identifier",
    )
    rename_group.add_argument(
        "--ignore-case",
        action="store_true",
        help="Use case-insensitive matching.",
    )
    rename_group.add_argument(
        "--all-matches",
        action="store_true",
        help="Rename every match in each file instead of only the first rule match.",
    )
    rename_group.add_argument(
        "--ignore-missing",
        action="store_true",
        help="Do not fail when a file has no rename matches.",
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
        help="Expected total number of rename matches.",
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

    if args.exclude_glob:
        filtered: list[Path] = []
        for path in ordered:
            path_text = str(path).replace("\\", "/")
            if any(fnmatch.fnmatch(path_text, pattern) for pattern in args.exclude_glob):
                continue
            filtered.append(path)
        ordered = filtered

    if not ordered:
        raise SystemExit("all target files were excluded")

    return ordered


def load_rules(args: argparse.Namespace) -> list[RenameRule]:
    has_pair = args.from_name is not None or args.to_name is not None
    has_map = args.map_file is not None
    if has_pair and has_map:
        raise SystemExit("use either --from-name/--to-name or --map-file, not both")
    if not has_pair and not has_map:
        raise SystemExit("provide --from-name/--to-name or --map-file")

    if has_pair:
        if args.from_name is None or args.to_name is None:
            raise SystemExit("both --from-name and --to-name are required")
        return [RenameRule(args.from_name, args.to_name, args.match_mode)]

    payload = json.loads(read_text(Path(args.map_file), args.encoding))
    rules: list[RenameRule] = []
    if isinstance(payload, dict):
        for source, target in payload.items():
            rules.append(RenameRule(str(source), str(target), args.match_mode))
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                raise SystemExit("map file list entries must be objects")
            source = item.get("from")
            target = item.get("to")
            mode = item.get("mode", args.match_mode)
            if source is None or target is None:
                raise SystemExit("map file entries require 'from' and 'to'")
            rules.append(RenameRule(str(source), str(target), str(mode)))
    else:
        raise SystemExit("map file must be a JSON object or a list of objects")

    if not rules:
        raise SystemExit("no rename rules were loaded")

    return rules


def compile_rule(rule: RenameRule, ignore_case: bool) -> re.Pattern[str]:
    flags = re.MULTILINE
    if ignore_case:
        flags |= re.IGNORECASE

    if rule.mode == "identifier":
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(rule.source)}(?![A-Za-z0-9_])"
    elif rule.mode == "word":
        pattern = rf"\b{re.escape(rule.source)}\b"
    elif rule.mode == "literal":
        pattern = re.escape(rule.source)
    elif rule.mode == "regex":
        pattern = rule.source
    else:
        raise SystemExit(f"unsupported match mode: {rule.mode}")

    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise SystemExit(f"invalid rename pattern {rule.source!r}: {exc}") from exc


def build_file_plan(path: Path, rules: list[RenameRule], args: argparse.Namespace) -> FilePlan | None:
    original_text = read_text(path, args.encoding)
    updated_text = original_text
    total_matches = 0

    for rule in rules:
        regex = compile_rule(rule, args.ignore_case)
        count = 0 if args.all_matches else 1
        updated_text, matches = regex.subn(rule.target, updated_text, count=count)
        total_matches += matches

    if total_matches == 0:
        if args.ignore_missing:
            return None
        raise SystemExit(f"no rename match found in {path}")

    if updated_text == original_text:
        return None

    return FilePlan(path=path, original_text=original_text, updated_text=updated_text, renames=total_matches)


def validate_expectations(plans: list[FilePlan], args: argparse.Namespace) -> None:
    if args.fail_on_noop and not plans:
        raise SystemExit("no changes would be made")

    if args.expect_total is not None:
        total = sum(plan.renames for plan in plans)
        if total != args.expect_total:
            raise SystemExit(f"total renames {total}, expected {args.expect_total}")

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
            f"# {plan.path} | renames={plan.renames} | "
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
        "total_renames": sum(plan.renames for plan in plans),
        "files": [
            {
                "path": str(plan.path),
                "renames": plan.renames,
                "bytes_delta": len(plan.updated_text) - len(plan.original_text),
            }
            for plan in plans
        ],
    }
    print(json.dumps(payload, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    targets = collect_target_paths(args)
    rules = load_rules(args)

    plans: list[FilePlan] = []
    for path in targets:
        plan = build_file_plan(path, rules, args)
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
