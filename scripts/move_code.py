#!/usr/bin/env python3
"""
move_code.py

Move a selected block of code from one file to another in a deterministic,
preview-first pass.

Features:
- selectors: literal snippet, regex, between markers, line range, simple symbol
- destination placement: start, end, before/after literal anchor, before/after regex anchor
- optional same-file moves
- optional source/destination backups
- unified diff preview and JSON summary
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
class Region:
    start: int
    end: int
    reason: str


@dataclass(slots=True)
class FilePlan:
    path: Path
    original_text: str
    updated_text: str


@dataclass(slots=True)
class MovePlan:
    source: FilePlan
    destination: FilePlan
    moved_text: str
    source_region: Region
    destination_insertions: int


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move code from one file to another with preview-first safety."
    )

    parser.add_argument("--from-file", required=True, help="Source file path.")
    parser.add_argument("--to-file", required=True, help="Destination file path.")

    selectors = parser.add_argument_group("selectors")
    selectors.add_argument("--specify", help="Literal code snippet to move.")
    selectors.add_argument("--specify-file", help="Read literal code snippet from a file.")
    selectors.add_argument("--regex", help="Regex snippet to move.")
    selectors.add_argument("--between", nargs=2, metavar=("START", "END"), help="Move text between two literal markers, inclusive.")
    selectors.add_argument("--line-range", help="Move a 1-based line range in START:END form.")
    selectors.add_argument("--symbol", help="Move a best-effort symbol block such as a C-like function.")
    selectors.add_argument("--ignore-missing", action="store_true", help="Treat a missing source selector as a no-op.")

    placement = parser.add_argument_group("destination placement")
    placement.add_argument("--at-start", action="store_true", help="Insert at the start of the destination.")
    placement.add_argument("--at-end", action="store_true", help="Insert at the end of the destination.")
    placement.add_argument("--before", help="Insert before this literal anchor.")
    placement.add_argument("--after", help="Insert after this literal anchor.")
    placement.add_argument("--before-regex", help="Insert before this regex anchor.")
    placement.add_argument("--after-regex", help="Insert after this regex anchor.")
    placement.add_argument("--all-matches", action="store_true", help="Insert around all destination anchor matches.")
    placement.add_argument("--ignore-missing-anchor", action="store_true", help="Treat a missing destination anchor as a no-op.")

    content = parser.add_argument_group("content shaping")
    content.add_argument("--ensure-leading-newline", action="store_true", help="Ensure moved text starts with a newline before insertion.")
    content.add_argument("--ensure-trailing-newline", action="store_true", help="Ensure moved text ends with a newline before insertion.")
    content.add_argument("--trim-source-gap", action="store_true", help="Collapse overly large blank-line gaps after removing from the source.")
    content.add_argument("--trim-destination-gap", action="store_true", help="Collapse overly large blank-line gaps after inserting into the destination.")

    behavior = parser.add_argument_group("behavior")
    behavior.add_argument("--mode", choices=["preview", "apply"], default=DEFAULT_MODE, help="Preview diffs or apply changes.")
    behavior.add_argument("--backup", action="store_true", help="Create .bak files in apply mode.")
    behavior.add_argument("--encoding", default=DEFAULT_ENCODING, help=f"Text encoding. Default: {DEFAULT_ENCODING}")
    behavior.add_argument("--context", type=int, default=3, help="Unified diff context lines.")
    behavior.add_argument("--ensure-parent", action="store_true", help="Create destination parent directories in apply mode.")
    behavior.add_argument("--force-create-destination", action="store_true", help="Allow a missing destination file to be created.")
    behavior.add_argument("--fail-on-noop", action="store_true", help="Exit with an error if no move would occur.")
    behavior.add_argument("--json-report", action="store_true", help="Emit a JSON summary after preview/apply output.")

    return parser.parse_args(argv)


def read_text(path: Path, encoding: str) -> str:
    try:
        return path.read_text(encoding=encoding)
    except FileNotFoundError:
        raise SystemExit(f"file not found: {path}")


def write_text(path: Path, text: str, encoding: str) -> None:
    with path.open("w", encoding=encoding, newline="") as handle:
        handle.write(text)


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


def normalize_gap(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


def line_range_to_offsets(text: str, raw: str) -> Region:
    if ":" not in raw:
        raise SystemExit("--line-range expects START:END")
    start_raw, end_raw = raw.split(":", 1)
    try:
        start_line = int(start_raw)
        end_line = int(end_raw)
    except ValueError as exc:
        raise SystemExit(f"invalid line range: {raw!r}") from exc
    if start_line <= 0 or end_line < start_line:
        raise SystemExit(f"invalid line range: {raw!r}")

    starts = [0]
    for match in re.finditer(r"\n", text):
        starts.append(match.end())
    starts.append(len(text) + 1)
    lines = text.splitlines(keepends=True)
    if end_line > len(lines):
        raise SystemExit(f"line range {raw!r} exceeds file length")
    start = starts[start_line - 1]
    end = starts[end_line] if end_line < len(starts) else len(text)
    return Region(start, end, "line-range")


def literal_region(text: str, needle: str) -> Region | None:
    index = text.find(needle)
    if index < 0:
        return None
    return Region(index, index + len(needle), "literal")


def regex_region(text: str, pattern: str) -> Region | None:
    try:
        regex = re.compile(pattern, re.MULTILINE | re.DOTALL)
    except re.error as exc:
        raise SystemExit(f"invalid regex {pattern!r}: {exc}") from exc
    match = regex.search(text)
    if not match:
        return None
    return Region(match.start(), match.end(), "regex")


def between_region(text: str, start_marker: str, end_marker: str) -> Region | None:
    start = text.find(start_marker)
    if start < 0:
        return None
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        return None
    return Region(start, end + len(end_marker), "between")


def is_symbol_boundary_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def find_matching_brace(text: str, open_index: int) -> int:
    depth = 0
    i = open_index
    in_string: str | None = None
    in_line_comment = False
    in_block_comment = False

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_string is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in {'"', "'"}:
            in_string = ch
            i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1

    raise SystemExit("unbalanced braces while scanning symbol")


def symbol_region(text: str, symbol: str) -> Region | None:
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])")
    match = pattern.search(text)
    if not match:
        return None

    brace_index = text.find("{", match.end())
    if brace_index < 0:
        return None

    scan = brace_index
    while scan >= 0 and text[scan] != "\n":
        scan -= 1
    start = scan + 1

    end_brace = find_matching_brace(text, brace_index)
    end = end_brace + 1
    while end < len(text) and text[end] in " \t":
        end += 1
    if end < len(text) and text[end] == "\r":
        end += 1
    if end < len(text) and text[end] == "\n":
        end += 1
    return Region(start, end, "symbol")


def load_source_region(text: str, args: argparse.Namespace) -> Region | None:
    selectors = sum(
        1
        for value in [
            args.specify is not None,
            args.specify_file is not None,
            args.regex is not None,
            args.between is not None,
            args.line_range is not None,
            args.symbol is not None,
        ]
        if value
    )
    if selectors != 1:
        raise SystemExit(
            "provide exactly one selector: --specify, --specify-file, --regex, --between, --line-range, or --symbol"
        )

    if args.specify is not None:
        return literal_region(text, args.specify)
    if args.specify_file is not None:
        return literal_region(text, read_text(Path(args.specify_file), args.encoding))
    if args.regex is not None:
        return regex_region(text, args.regex)
    if args.between is not None:
        return between_region(text, args.between[0], args.between[1])
    if args.line_range is not None:
        return line_range_to_offsets(text, args.line_range)
    return symbol_region(text, args.symbol)


def determine_placement(args: argparse.Namespace) -> str:
    selected = [
        args.at_start,
        args.at_end,
        args.before is not None,
        args.after is not None,
        args.before_regex is not None,
        args.after_regex is not None,
    ]
    if sum(1 for item in selected if item) != 1:
        raise SystemExit(
            "choose exactly one destination placement: --at-start, --at-end, --before, --after, --before-regex, or --after-regex"
        )
    if args.at_start:
        return "start"
    if args.at_end:
        return "end"
    if args.before is not None:
        return "before"
    if args.after is not None:
        return "after"
    if args.before_regex is not None:
        return "before-regex"
    return "after-regex"


def find_anchor_matches(text: str, placement: str, args: argparse.Namespace) -> list[tuple[int, int]]:
    if placement == "before":
        anchor = args.before
        assert anchor is not None
        matches = []
        start = 0
        while True:
            index = text.find(anchor, start)
            if index < 0:
                break
            matches.append((index, index + len(anchor)))
            start = index + len(anchor)
        return matches
    if placement == "after":
        anchor = args.after
        assert anchor is not None
        matches = []
        start = 0
        while True:
            index = text.find(anchor, start)
            if index < 0:
                break
            matches.append((index, index + len(anchor)))
            start = index + len(anchor)
        return matches
    pattern = args.before_regex if placement == "before-regex" else args.after_regex
    assert pattern is not None
    try:
        regex = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise SystemExit(f"invalid destination regex {pattern!r}: {exc}") from exc
    return [(match.start(), match.end()) for match in regex.finditer(text)]


def insert_text(text: str, moved_text: str, placement: str, args: argparse.Namespace) -> tuple[str, int]:
    if placement == "start":
        return moved_text + text, 1
    if placement == "end":
        return text + moved_text, 1

    matches = find_anchor_matches(text, placement, args)
    if not matches:
        if args.ignore_missing_anchor:
            return text, 0
        needle = args.before or args.after or args.before_regex or args.after_regex
        raise SystemExit(f"destination anchor not found: {needle!r}")

    if not args.all_matches:
        matches = matches[:1]

    updated = text
    offset = 0
    for start, end in matches:
        insertion_index = start if placement in {"before", "before-regex"} else end
        insertion_index += offset
        updated = updated[:insertion_index] + moved_text + updated[insertion_index:]
        offset += len(moved_text)
    return updated, len(matches)


def build_plan(args: argparse.Namespace) -> MovePlan | None:
    source_path = Path(args.from_file)
    destination_path = Path(args.to_file)
    source_text = read_text(source_path, args.encoding)

    if destination_path.exists():
        if destination_path.is_dir():
            raise SystemExit(f"destination is a directory, not a file: {destination_path}")
        destination_text = read_text(destination_path, args.encoding)
    else:
        if not args.force_create_destination:
            raise SystemExit(
                f"destination file does not exist: {destination_path}. Use --force-create-destination to allow creation."
            )
        destination_text = ""

    region = load_source_region(source_text, args)
    if region is None:
        if args.ignore_missing:
            return None
        raise SystemExit("source selector not found")

    moved_text = source_text[region.start:region.end]
    if args.ensure_leading_newline and moved_text and not moved_text.startswith("\n"):
        moved_text = "\n" + moved_text
    if args.ensure_trailing_newline and moved_text and not moved_text.endswith("\n"):
        moved_text += "\n"

    source_updated = source_text[:region.start] + source_text[region.end:]
    if args.trim_source_gap:
        source_updated = normalize_gap(source_updated)

    placement = determine_placement(args)
    destination_updated, insertions = insert_text(destination_text, moved_text, placement, args)
    if args.trim_destination_gap:
        destination_updated = normalize_gap(destination_updated)

    source_changed = source_updated != source_text
    destination_changed = destination_updated != destination_text
    if not source_changed and not destination_changed:
        return None

    return MovePlan(
        source=FilePlan(source_path, source_text, source_updated),
        destination=FilePlan(destination_path, destination_text, destination_updated),
        moved_text=moved_text,
        source_region=region,
        destination_insertions=insertions,
    )


def print_preview(plan: MovePlan, args: argparse.Namespace) -> None:
    print(
        f"# move {plan.source.path} -> {plan.destination.path} | selector={plan.source_region.reason} | "
        f"moved_bytes={len(plan.moved_text)} | destination_insertions={plan.destination_insertions}"
    )
    print("\n## Source diff")
    print(unified_diff(plan.source.path, plan.source.original_text, plan.source.updated_text, args.context))
    print("\n## Destination diff")
    print(unified_diff(plan.destination.path, plan.destination.original_text, plan.destination.updated_text, args.context))


def apply_plan(plan: MovePlan, args: argparse.Namespace) -> None:
    if args.backup:
        shutil.copyfile(plan.source.path, Path(str(plan.source.path) + ".bak"))
        if plan.destination.path.exists() and plan.destination.path != plan.source.path:
            shutil.copyfile(plan.destination.path, Path(str(plan.destination.path) + ".bak"))

    if args.ensure_parent:
        plan.destination.path.parent.mkdir(parents=True, exist_ok=True)

    write_text(plan.source.path, plan.source.updated_text, args.encoding)
    write_text(plan.destination.path, plan.destination.updated_text, args.encoding)


def emit_json_report(plan: MovePlan | None) -> None:
    payload = {
        "moved": plan is not None,
        "source": None if plan is None else str(plan.source.path),
        "destination": None if plan is None else str(plan.destination.path),
        "selector": None if plan is None else plan.source_region.reason,
        "moved_bytes": 0 if plan is None else len(plan.moved_text),
        "destination_insertions": 0 if plan is None else plan.destination_insertions,
    }
    print(json.dumps(payload, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    plan = build_plan(args)

    if args.fail_on_noop and plan is None:
        raise SystemExit("no move would be made")

    if args.mode == "preview":
        if plan is None:
            print("No changes.")
        else:
            print_preview(plan, args)
    else:
        if plan is None:
            print("No changes.")
        else:
            apply_plan(plan, args)
            print("Applied move.")

    if args.json_report:
        emit_json_report(plan)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
