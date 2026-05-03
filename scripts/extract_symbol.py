#!/usr/bin/env python3
"""
extract_symbol.py

Extract a symbol block from a source file into another file.

This is a focused refactor helper for common C/C++-style extraction work:
- function blocks via brace scanning
- struct/enum/union blocks via brace scanning
- typedef blocks ending at ';'
- optional declaration emission to another file
- optional replacement text left behind in the source
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
    kind: str


@dataclass(slots=True)
class FilePlan:
    path: Path
    original_text: str
    updated_text: str


@dataclass(slots=True)
class ExtractionPlan:
    source: FilePlan
    destination: FilePlan
    declaration: FilePlan | None
    symbol: str
    kind: str
    moved_text: str
    declaration_text: str | None


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a symbol block from one file into another with preview-first safety."
    )
    parser.add_argument("--from-file", required=True, help="Source file path.")
    parser.add_argument("--to-file", required=True, help="Destination file path.")
    parser.add_argument("--symbol", required=True, help="Symbol name to extract.")
    parser.add_argument(
        "--kind",
        choices=["function", "struct", "enum", "union", "typedef", "auto"],
        default="auto",
        help="Symbol kind. Default: auto",
    )
    parser.add_argument(
        "--placement",
        choices=["start", "end"],
        default="end",
        help="Where to place the extracted symbol in the destination file.",
    )
    parser.add_argument(
        "--declaration-file",
        help="Optional file to receive a generated declaration/prototype.",
    )
    parser.add_argument(
        "--leave-behind",
        default="",
        help="Replacement text to leave behind in the source after extraction.",
    )
    parser.add_argument(
        "--declaration-style",
        choices=["auto", "prototype", "verbatim"],
        default="auto",
        help="How to derive the declaration text when --declaration-file is used.",
    )
    parser.add_argument("--ensure-parent", action="store_true", help="Create destination parent directories in apply mode.")
    parser.add_argument("--force-create-destination", action="store_true", help="Allow missing destination/declaration files to be created.")
    parser.add_argument("--trim-gaps", action="store_true", help="Collapse overly large blank-line gaps after extraction/insertion.")
    parser.add_argument("--mode", choices=["preview", "apply"], default=DEFAULT_MODE, help="Preview diffs or apply changes.")
    parser.add_argument("--backup", action="store_true", help="Create .bak files in apply mode.")
    parser.add_argument("--encoding", default=DEFAULT_ENCODING, help=f"Text encoding. Default: {DEFAULT_ENCODING}")
    parser.add_argument("--context", type=int, default=3, help="Unified diff context lines.")
    parser.add_argument("--fail-on-noop", action="store_true", help="Exit with an error if no extraction would occur.")
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


def find_statement_start(text: str, token_index: int) -> int:
    scan = token_index
    while scan > 0 and text[scan - 1] != "\n":
        scan -= 1
    return scan


def consume_trailing_newline(text: str, end: int) -> int:
    while end < len(text) and text[end] in " \t":
        end += 1
    if end < len(text) and text[end] == "\r":
        end += 1
    if end < len(text) and text[end] == "\n":
        end += 1
    return end


def detect_kind(text: str, symbol: str) -> str:
    candidates = [
        ("function", re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}\s*\(")),
        ("typedef", re.compile(rf"typedef\b[\s\S]*?\b{re.escape(symbol)}\b")),
        ("struct", re.compile(rf"struct\s+{re.escape(symbol)}\b")),
        ("enum", re.compile(rf"enum\s+{re.escape(symbol)}\b")),
        ("union", re.compile(rf"union\s+{re.escape(symbol)}\b")),
    ]
    for kind, pattern in candidates:
        if pattern.search(text):
            return kind
    raise SystemExit(f"could not infer symbol kind for {symbol!r}; pass --kind explicitly")


def function_region(text: str, symbol: str) -> Region | None:
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}\s*\(")
    match = pattern.search(text)
    if not match:
        return None
    brace_index = text.find("{", match.end())
    if brace_index < 0:
        return None
    start = find_statement_start(text, match.start())
    end = consume_trailing_newline(text, find_matching_brace(text, brace_index) + 1)
    return Region(start, end, "function")


def aggregate_region(text: str, kind: str, symbol: str) -> Region | None:
    pattern = re.compile(rf"{kind}\s+{re.escape(symbol)}\b")
    match = pattern.search(text)
    if not match:
        return None
    brace_index = text.find("{", match.end())
    if brace_index < 0:
        return None
    end_brace = find_matching_brace(text, brace_index)
    semicolon = text.find(";", end_brace)
    if semicolon < 0:
        return None
    start = find_statement_start(text, match.start())
    end = consume_trailing_newline(text, semicolon + 1)
    return Region(start, end, kind)


def typedef_region(text: str, symbol: str) -> Region | None:
    pattern = re.compile(rf"typedef\b[\s\S]*?\b{re.escape(symbol)}\b[\s\S]*?;", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    start = find_statement_start(text, match.start())
    end = consume_trailing_newline(text, match.end())
    return Region(start, end, "typedef")


def locate_region(text: str, symbol: str, kind: str) -> Region | None:
    actual_kind = detect_kind(text, symbol) if kind == "auto" else kind
    if actual_kind == "function":
        return function_region(text, symbol)
    if actual_kind in {"struct", "enum", "union"}:
        return aggregate_region(text, actual_kind, symbol)
    if actual_kind == "typedef":
        return typedef_region(text, symbol)
    raise SystemExit(f"unsupported extraction kind: {actual_kind}")


def derive_declaration(moved_text: str, kind: str, style: str) -> str:
    if style == "verbatim":
        return moved_text if moved_text.endswith("\n") else moved_text + "\n"

    actual_style = style
    if actual_style == "auto":
        actual_style = "prototype" if kind == "function" else "verbatim"

    if actual_style == "prototype":
        header, sep, _body = moved_text.partition("{")
        if not sep:
            return moved_text if moved_text.endswith("\n") else moved_text + "\n"
        prototype = header.rstrip()
        if not prototype.endswith(";"):
            prototype += ";"
        return prototype + "\n"

    return moved_text if moved_text.endswith("\n") else moved_text + "\n"


def destination_text(path: Path, encoding: str, allow_create: bool) -> str:
    if path.exists():
        if path.is_dir():
            raise SystemExit(f"destination is a directory, not a file: {path}")
        return read_text(path, encoding)
    if allow_create:
        return ""
    raise SystemExit(f"destination file does not exist: {path}")


def build_plan(args: argparse.Namespace) -> ExtractionPlan | None:
    source_path = Path(args.from_file)
    destination_path = Path(args.to_file)
    declaration_path = Path(args.declaration_file) if args.declaration_file else None

    source_original = read_text(source_path, args.encoding)
    destination_original = destination_text(destination_path, args.encoding, args.force_create_destination)
    declaration_original = None if declaration_path is None else destination_text(declaration_path, args.encoding, args.force_create_destination)

    region = locate_region(source_original, args.symbol, args.kind)
    if region is None:
        return None

    kind = region.kind
    moved_text = source_original[region.start:region.end]
    source_updated = source_original[:region.start] + args.leave_behind + source_original[region.end:]
    destination_updated = moved_text + destination_original if args.placement == "start" else destination_original + moved_text

    declaration_text = None
    declaration_updated = None
    if declaration_path is not None:
        declaration_text = derive_declaration(moved_text, kind, args.declaration_style)
        declaration_updated = declaration_original + declaration_text

    if args.trim_gaps:
        source_updated = normalize_gap(source_updated)
        destination_updated = normalize_gap(destination_updated)
        if declaration_updated is not None:
            declaration_updated = normalize_gap(declaration_updated)

    source_plan = FilePlan(source_path, source_original, source_updated)
    destination_plan = FilePlan(destination_path, destination_original, destination_updated)
    declaration_plan = None
    if declaration_path is not None and declaration_original is not None and declaration_updated is not None:
        declaration_plan = FilePlan(declaration_path, declaration_original, declaration_updated)

    changed = source_updated != source_original or destination_updated != destination_original
    if declaration_plan is not None:
        changed = changed or declaration_plan.updated_text != declaration_plan.original_text
    if not changed:
        return None

    return ExtractionPlan(
        source=source_plan,
        destination=destination_plan,
        declaration=declaration_plan,
        symbol=args.symbol,
        kind=kind,
        moved_text=moved_text,
        declaration_text=declaration_text,
    )


def print_preview(plan: ExtractionPlan, args: argparse.Namespace) -> None:
    print(f"# extract {plan.symbol} ({plan.kind})")
    print("\n## Source diff")
    print(unified_diff(plan.source.path, plan.source.original_text, plan.source.updated_text, args.context))
    print("\n## Destination diff")
    print(unified_diff(plan.destination.path, plan.destination.original_text, plan.destination.updated_text, args.context))
    if plan.declaration is not None:
        print("\n## Declaration diff")
        print(unified_diff(plan.declaration.path, plan.declaration.original_text, plan.declaration.updated_text, args.context))


def backup_if_needed(path: Path, enabled: bool) -> None:
    if enabled and path.exists():
        shutil.copyfile(path, Path(str(path) + ".bak"))


def apply_plan(plan: ExtractionPlan, args: argparse.Namespace) -> None:
    if args.ensure_parent:
        plan.destination.path.parent.mkdir(parents=True, exist_ok=True)
        if plan.declaration is not None:
            plan.declaration.path.parent.mkdir(parents=True, exist_ok=True)

    backup_if_needed(plan.source.path, args.backup)
    backup_if_needed(plan.destination.path, args.backup)
    if plan.declaration is not None:
        backup_if_needed(plan.declaration.path, args.backup)

    write_text(plan.source.path, plan.source.updated_text, args.encoding)
    write_text(plan.destination.path, plan.destination.updated_text, args.encoding)
    if plan.declaration is not None:
        write_text(plan.declaration.path, plan.declaration.updated_text, args.encoding)


def emit_json_report(plan: ExtractionPlan | None) -> None:
    payload = {
        "extracted": plan is not None,
        "symbol": None if plan is None else plan.symbol,
        "kind": None if plan is None else plan.kind,
        "source": None if plan is None else str(plan.source.path),
        "destination": None if plan is None else str(plan.destination.path),
        "declaration_file": None if plan is None or plan.declaration is None else str(plan.declaration.path),
        "moved_bytes": 0 if plan is None else len(plan.moved_text),
    }
    print(json.dumps(payload, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    plan = build_plan(args)

    if args.fail_on_noop and plan is None:
        raise SystemExit("no extraction would be made")

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
            print("Applied extraction.")

    if args.json_report:
        emit_json_report(plan)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
