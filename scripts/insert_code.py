#!/usr/bin/env python3
"""
insert_code.py

Deterministic bulk insertion helper.

- preview is the default mode
- inserts literal content before or after literal/regex anchors
- supports start/end insertion modes
- can skip duplicate insertion and enforce expected insertion counts
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import sys
import textwrap
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
    insertions: int
    mode: str


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Insert code into one or many files with preview-first safety."
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

    content_group = parser.add_argument_group("content")
    content_group.add_argument("--content", help="Literal content to insert.")
    content_group.add_argument("--content-file", help="Read content to insert from a file.")
    content_group.add_argument(
        "--stdin",
        action="store_true",
        help="Read content to insert from standard input.",
    )
    content_group.add_argument("--dedent", action="store_true", help="Dedent inserted content.")
    content_group.add_argument(
        "--strip-trailing-whitespace",
        action="store_true",
        help="Strip trailing whitespace from inserted lines.",
    )
    content_group.add_argument(
        "--trim-leading-blank-lines",
        action="store_true",
        help="Trim leading blank lines from inserted content.",
    )
    content_group.add_argument(
        "--trim-trailing-blank-lines",
        action="store_true",
        help="Trim trailing blank lines from inserted content.",
    )
    content_group.add_argument(
        "--ensure-leading-newline",
        action="store_true",
        help="Ensure the inserted content starts with a newline.",
    )
    content_group.add_argument(
        "--ensure-trailing-newline",
        action="store_true",
        help="Ensure the inserted content ends with a newline.",
    )
    content_group.add_argument(
        "--if-not-present",
        help="Skip files where this literal snippet already exists.",
    )

    anchor_group = parser.add_argument_group("placement")
    anchor_group.add_argument("--before", help="Insert before this literal anchor.")
    anchor_group.add_argument("--after", help="Insert after this literal anchor.")
    anchor_group.add_argument("--before-regex", help="Insert before this regex anchor.")
    anchor_group.add_argument("--after-regex", help="Insert after this regex anchor.")
    anchor_group.add_argument(
        "--at-start",
        action="store_true",
        help="Insert at the start of the file.",
    )
    anchor_group.add_argument(
        "--at-end",
        action="store_true",
        help="Insert at the end of the file.",
    )
    anchor_group.add_argument(
        "--all-matches",
        action="store_true",
        help="Insert at every anchor match instead of only the first.",
    )
    anchor_group.add_argument(
        "--ignore-missing",
        action="store_true",
        help="Treat a missing anchor as a no-op instead of an error.",
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
        help="Expected total insertions across all changed files.",
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


def load_insert_text(args: argparse.Namespace, stdin_text: str | None) -> str:
    provided = sum(
        1
        for flag in [args.content is not None, args.content_file is not None, args.stdin]
        if flag
    )
    if provided != 1:
        raise SystemExit("provide exactly one of --content, --content-file, or --stdin")

    if args.content is not None:
        text = args.content
    elif args.content_file is not None:
        text = read_text(Path(args.content_file), args.encoding)
    else:
        text = stdin_text or ""

    if args.dedent:
        text = textwrap.dedent(text)
    if args.trim_leading_blank_lines:
        text = re.sub(r"\A(?:[ \t]*\n)+", "", text)
    if args.trim_trailing_blank_lines:
        text = re.sub(r"(?:\n[ \t]*)+\Z", "", text)
    if args.strip_trailing_whitespace:
        text = "\n".join(line.rstrip(" \t") for line in text.splitlines())
    if args.ensure_leading_newline and not text.startswith("\n"):
        text = "\n" + text
    if args.ensure_trailing_newline and text and not text.endswith("\n"):
        text += "\n"
    return text


def placement_mode(args: argparse.Namespace) -> str:
    selected = [
        args.before is not None,
        args.after is not None,
        args.before_regex is not None,
        args.after_regex is not None,
        args.at_start,
        args.at_end,
    ]
    if sum(1 for flag in selected if flag) != 1:
        raise SystemExit(
            "choose exactly one placement mode: --before, --after, --before-regex, --after-regex, --at-start, or --at-end"
        )
    if args.before is not None:
        return "before"
    if args.after is not None:
        return "after"
    if args.before_regex is not None:
        return "before-regex"
    if args.after_regex is not None:
        return "after-regex"
    if args.at_start:
        return "at-start"
    return "at-end"


def find_literal_matches(text: str, anchor: str) -> list[tuple[int, int]]:
    if anchor == "":
        raise SystemExit("literal anchor must not be empty")
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        index = text.find(anchor, start)
        if index < 0:
            break
        matches.append((index, index + len(anchor)))
        start = index + len(anchor)
    return matches


def find_regex_matches(text: str, pattern: str) -> list[tuple[int, int]]:
    try:
        regex = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise SystemExit(f"invalid regex anchor {pattern!r}: {exc}") from exc
    return [(match.start(), match.end()) for match in regex.finditer(text)]


def insert_around_matches(
    text: str,
    insert_text: str,
    matches: list[tuple[int, int]],
    *,
    insert_before: bool,
    all_matches: bool,
) -> tuple[str, int]:
    if not all_matches:
        matches = matches[:1]

    updated = text
    offset = 0
    for start, end in matches:
        position = start if insert_before else end
        insertion_index = position + offset
        updated = updated[:insertion_index] + insert_text + updated[insertion_index:]
        offset += len(insert_text)
    return updated, len(matches)


def build_file_plan(path: Path, insert_text: str, mode_name: str, args: argparse.Namespace) -> FilePlan | None:
    original_text = read_text(path, args.encoding)

    if args.if_not_present and args.if_not_present in original_text:
        return None

    if mode_name == "at-start":
        updated_text = insert_text + original_text
        insertions = 1
    elif mode_name == "at-end":
        updated_text = original_text + insert_text
        insertions = 1
    else:
        matches = (
            find_literal_matches(original_text, args.before or args.after)
            if mode_name in {"before", "after"}
            else find_regex_matches(original_text, args.before_regex or args.after_regex)
        )
        if not matches:
            if args.ignore_missing:
                return None
            needle = args.before or args.after or args.before_regex or args.after_regex
            raise SystemExit(f"anchor not found in {path}: {needle!r}")
        updated_text, insertions = insert_around_matches(
            original_text,
            insert_text,
            matches,
            insert_before=mode_name in {"before", "before-regex"},
            all_matches=args.all_matches,
        )

    if updated_text == original_text:
        return None

    return FilePlan(
        path=path,
        original_text=original_text,
        updated_text=updated_text,
        insertions=insertions,
        mode=mode_name,
    )


def validate_expectations(plans: list[FilePlan], args: argparse.Namespace) -> None:
    if args.fail_on_noop and not plans:
        raise SystemExit("no changes would be made")

    if args.expect_total is not None:
        total = sum(plan.insertions for plan in plans)
        if total != args.expect_total:
            raise SystemExit(f"total insertions {total}, expected {args.expect_total}")

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
            f"# {plan.path} | mode={plan.mode} | insertions={plan.insertions} | "
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
        "total_insertions": sum(plan.insertions for plan in plans),
        "files": [
            {
                "path": str(plan.path),
                "mode": plan.mode,
                "insertions": plan.insertions,
                "bytes_delta": len(plan.updated_text) - len(plan.original_text),
            }
            for plan in plans
        ],
    }
    print(json.dumps(payload, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    stdin_text = sys.stdin.read() if args.stdin else None
    targets = collect_target_paths(args)
    insert_text = load_insert_text(args, stdin_text)
    mode_name = placement_mode(args)

    plans: list[FilePlan] = []
    for path in targets:
        plan = build_file_plan(path, insert_text, mode_name, args)
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
