#!/usr/bin/env python3
"""
remove_code.py

Deterministic refactor helper for removing code from one or many files in one
pass. This is intentionally stricter than a search-and-replace script:

- preview is the default mode
- every removal can be targeted by literal text, regex, ranges, markers, or
  simple function names
- the script can fail on no-op so it does not silently "succeed"
- optional backups are supported for destructive runs

Examples:

    python ai_tools/remove_code.py ^
      --bulk "[src/game/a.c,src/game/b.c]" ^
      --specify "extern bool gDjuiInMainMenu;" ^
      --mode preview

    python ai_tools/remove_code.py ^
      --files src/pc/djui/djui.c src/pc/djui/djui_panel_pause.c ^
      --regex "djui_panel_main_create\\(NULL\\);\\s*" ^
      --all-matches ^
      --mode apply ^
      --backup

    python ai_tools/remove_code.py ^
      --glob "src/game/*.c" ^
      --between "/* OLD UI START */" "/* OLD UI END */" ^
      --mode apply

    python ai_tools/remove_code.py ^
      --files src/game/ingame_menu.c ^
      --line-range 2988:2995 ^
      --trim-blank-lines ^
      --mode apply
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_MODE = "preview"


@dataclass(slots=True)
class MatchRegion:
    start: int
    end: int
    reason: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class FilePlan:
    path: Path
    original_text: str
    updated_text: str
    removals: list[MatchRegion]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove code from multiple files in a deterministic bulk pass."
    )

    target_group = parser.add_argument_group("targets")
    target_group.add_argument(
        "--files",
        nargs="+",
        default=[],
        help="Explicit file paths to edit.",
    )
    target_group.add_argument(
        "--bulk",
        action="append",
        default=[],
        help=(
            "Bulk file list. Accepts JSON arrays like "
            "'[file1.c,file2.c]' or '[\"file1.c\",\"file2.c\"]', or comma-separated paths."
        ),
    )
    target_group.add_argument(
        "--glob",
        action="append",
        default=[],
        help="Glob pattern to expand from the current working directory.",
    )
    target_group.add_argument(
        "--file-list",
        action="append",
        default=[],
        help="Text file containing one path per line.",
    )

    selector_group = parser.add_argument_group("selectors")
    selector_group.add_argument(
        "--specify",
        action="append",
        default=[],
        help="Literal code snippet to remove.",
    )
    selector_group.add_argument(
        "--specify-file",
        action="append",
        default=[],
        help="Read a literal code snippet to remove from a file.",
    )
    selector_group.add_argument(
        "--regex",
        action="append",
        default=[],
        help="Regex pattern to remove.",
    )
    selector_group.add_argument(
        "--line-range",
        action="append",
        default=[],
        help="1-based inclusive line range like START:END.",
    )
    selector_group.add_argument(
        "--between",
        nargs=2,
        action="append",
        metavar=("START", "END"),
        default=[],
        help="Remove text between two literal anchors, including the anchors.",
    )
    selector_group.add_argument(
        "--marker-block",
        nargs=2,
        action="append",
        metavar=("START_MARKER", "END_MARKER"),
        default=[],
        help="Alias for --between when removing marker regions.",
    )
    selector_group.add_argument(
        "--symbol",
        action="append",
        default=[],
        help=(
            "Best-effort C/C++/JS function or block name to remove. "
            "This is brace-scanned, not AST-based."
        ),
    )
    selector_group.add_argument(
        "--include-line",
        action="append",
        default=[],
        help="Remove include/import lines containing this substring.",
    )
    selector_group.add_argument(
        "--empty-preprocessor-guard",
        action="store_true",
        help="After removals, also remove trivial empty #if/#endif wrappers left behind.",
    )

    behavior_group = parser.add_argument_group("behavior")
    behavior_group.add_argument(
        "--mode",
        choices=("preview", "apply"),
        default=DEFAULT_MODE,
        help="Preview changes or write them to disk.",
    )
    behavior_group.add_argument(
        "--all-matches",
        action="store_true",
        help="Remove every match for each selector. Default is first match per selector.",
    )
    behavior_group.add_argument(
        "--ignore-missing",
        action="store_true",
        help="Do not fail when a selector has no match in a target file.",
    )
    behavior_group.add_argument(
        "--fail-on-noop",
        action="store_true",
        help="Fail if no file changes would be made.",
    )
    behavior_group.add_argument(
        "--expect-per-file",
        type=int,
        default=None,
        help="Require an exact removal count per changed file.",
    )
    behavior_group.add_argument(
        "--expect-total",
        type=int,
        default=None,
        help="Require an exact total removal count across all files.",
    )
    behavior_group.add_argument(
        "--backup",
        action="store_true",
        help="Write .bak backups before applying edits.",
    )
    behavior_group.add_argument(
        "--trim-blank-lines",
        action="store_true",
        help="Collapse oversized blank-line runs after removals.",
    )
    behavior_group.add_argument(
        "--encoding",
        default="utf-8",
        help="Text encoding for file IO. Default: utf-8",
    )
    behavior_group.add_argument(
        "--context",
        type=int,
        default=3,
        help="Unified diff context lines for preview output.",
    )
    behavior_group.add_argument(
        "--json-report",
        action="store_true",
        help="Print a machine-readable JSON summary after execution.",
    )

    args = parser.parse_args(argv)

    if not has_any_selector(args):
        parser.error("at least one selector is required")

    return args


def has_any_selector(args: argparse.Namespace) -> bool:
    return any(
        (
            args.specify,
            args.specify_file,
            args.regex,
            args.line_range,
            args.between,
            args.marker_block,
            args.symbol,
            args.include_line,
        )
    )


def read_text(path: Path, encoding: str) -> str:
    return path.read_text(encoding=encoding)


def write_text(path: Path, text: str, encoding: str) -> None:
    path.write_text(text, encoding=encoding, newline="")


def parse_bulk_value(value: str) -> list[str]:
    stripped = value.strip()
    if not stripped:
        return []

    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped
        try:
            parsed = json.loads(inner)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            inner = stripped[1:-1]
            if not inner.strip():
                return []
            return [piece.strip().strip("\"'") for piece in inner.split(",") if piece.strip()]

    if "," in stripped:
        return [piece.strip() for piece in stripped.split(",") if piece.strip()]

    return [stripped]


def collect_target_paths(args: argparse.Namespace) -> list[Path]:
    collected: list[Path] = []

    for item in args.files:
        collected.append(Path(item))

    for bulk in args.bulk:
        for item in parse_bulk_value(bulk):
            collected.append(Path(item))

    cwd = Path.cwd()
    for pattern in args.glob:
        collected.extend(sorted(cwd.glob(pattern)))

    for list_file in args.file_list:
        list_path = Path(list_file)
        if not list_path.exists():
            raise SystemExit(f"file list does not exist: {list_path}")
        for line in read_text(list_path, args.encoding).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            collected.append(Path(stripped))

    unique: list[Path] = []
    seen: set[Path] = set()
    for raw_path in collected:
        path = raw_path if raw_path.is_absolute() else cwd / raw_path
        path = path.resolve()
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)

    if not unique:
        raise SystemExit("no target files were resolved")

    missing = [path for path in unique if not path.exists()]
    if missing:
        formatted = "\n".join(str(path) for path in missing)
        raise SystemExit(f"some target files do not exist:\n{formatted}")

    directories = [path for path in unique if path.is_dir()]
    if directories:
        formatted = "\n".join(str(path) for path in directories)
        raise SystemExit(f"directory targets are not supported:\n{formatted}")

    return unique


def load_literal_specs(args: argparse.Namespace) -> list[str]:
    specs = list(args.specify)
    for snippet_file in args.specify_file:
        path = Path(snippet_file)
        if not path.exists():
            raise SystemExit(f"specify-file does not exist: {path}")
        specs.append(read_text(path, args.encoding))
    return specs


def find_literal_regions(text: str, snippet: str, all_matches: bool) -> list[MatchRegion]:
    regions: list[MatchRegion] = []
    if snippet == "":
        return regions

    start = 0
    while True:
        index = text.find(snippet, start)
        if index < 0:
            break
        regions.append(
            MatchRegion(
                start=index,
                end=index + len(snippet),
                reason="literal",
                details={"snippet": snippet},
            )
        )
        if not all_matches:
            break
        start = index + len(snippet)
    return regions


def find_regex_regions(text: str, pattern: str, all_matches: bool) -> list[MatchRegion]:
    try:
        compiled = re.compile(pattern, re.MULTILINE | re.DOTALL)
    except re.error as exc:
        raise SystemExit(f"invalid regex '{pattern}': {exc}") from exc

    regions: list[MatchRegion] = []
    for match in compiled.finditer(text):
        if match.start() == match.end():
            continue
        regions.append(
            MatchRegion(
                start=match.start(),
                end=match.end(),
                reason="regex",
                details={"pattern": pattern},
            )
        )
        if not all_matches:
            break
    return regions


def line_start_offsets(text: str) -> list[int]:
    offsets = [0]
    for match in re.finditer(r"\n", text):
        offsets.append(match.end())
    return offsets


def parse_line_range(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+):(\d+)", value.strip())
    if not match:
        raise SystemExit(f"invalid --line-range '{value}', expected START:END")
    start = int(match.group(1))
    end = int(match.group(2))
    if start < 1 or end < start:
        raise SystemExit(f"invalid --line-range '{value}', expected positive ascending range")
    return start, end


def find_line_range_region(text: str, start_line: int, end_line: int) -> MatchRegion:
    offsets = line_start_offsets(text)
    total_lines = len(text.splitlines()) or 1
    if start_line > total_lines:
        raise SystemExit(
            f"--line-range start {start_line} exceeds file line count {total_lines}"
        )

    start_index = offsets[start_line - 1]
    if end_line < total_lines:
        end_index = offsets[end_line]
    else:
        end_index = len(text)

    return MatchRegion(
        start=start_index,
        end=end_index,
        reason="line-range",
        details={"start_line": start_line, "end_line": end_line},
    )


def find_between_regions(text: str, start_marker: str, end_marker: str, all_matches: bool) -> list[MatchRegion]:
    regions: list[MatchRegion] = []
    search_from = 0

    while True:
        start = text.find(start_marker, search_from)
        if start < 0:
            break
        end_search = start + len(start_marker)
        end = text.find(end_marker, end_search)
        if end < 0:
            raise SystemExit(
                f"start marker found without end marker: {start_marker!r} -> {end_marker!r}"
            )
        region_end = end + len(end_marker)
        regions.append(
            MatchRegion(
                start=start,
                end=region_end,
                reason="between",
                details={"start_marker": start_marker, "end_marker": end_marker},
            )
        )
        if not all_matches:
            break
        search_from = region_end

    return regions


def find_include_regions(text: str, needle: str, all_matches: bool) -> list[MatchRegion]:
    regions: list[MatchRegion] = []
    for match in re.finditer(r"^.*" + re.escape(needle) + r".*(?:\n|$)", text, re.MULTILINE):
        regions.append(
            MatchRegion(
                start=match.start(),
                end=match.end(),
                reason="include-line",
                details={"contains": needle},
            )
        )
        if not all_matches:
            break
    return regions


def find_symbol_regions(text: str, symbol: str, all_matches: bool) -> list[MatchRegion]:
    patterns = [
        re.compile(rf"(?m)^[^\n]*\b{re.escape(symbol)}\s*\([^;{{\n]*\)\s*\{{"),
        re.compile(rf"(?m)^[^\n]*\b{re.escape(symbol)}\b[^\n=]*=\s*\{{"),
        re.compile(rf"(?m)^[^\n]*\b{re.escape(symbol)}\b[^\n]*\{{"),
    ]

    regions: list[MatchRegion] = []
    used_starts: set[int] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            start = match.start()
            if start in used_starts:
                continue
            brace_start = text.find("{", match.start(), match.end())
            if brace_start < 0:
                continue
            brace_end = scan_balanced_braces(text, brace_start)
            if brace_end < 0:
                continue
            region_end = include_trailing_semicolon_and_space(text, brace_end + 1)
            region_start = extend_region_to_preceding_blank_line(text, start)
            regions.append(
                MatchRegion(
                    start=region_start,
                    end=region_end,
                    reason="symbol",
                    details={"symbol": symbol},
                )
            )
            used_starts.add(start)
            if not all_matches:
                return regions
    return regions


def scan_balanced_braces(text: str, brace_start: int) -> int:
    depth = 0
    i = brace_start
    in_line_comment = False
    in_block_comment = False
    in_string: str | None = None

    while i < len(text):
        char = text[i]
        next_two = text[i : i + 2]

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if next_two == "*/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue

        if in_string is not None:
            if char == "\\":
                i += 2
                continue
            if char == in_string:
                in_string = None
            i += 1
            continue

        if next_two == "//":
            in_line_comment = True
            i += 2
            continue
        if next_two == "/*":
            in_block_comment = True
            i += 2
            continue
        if char in ("'", '"', "`"):
            in_string = char
            i += 1
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1

    return -1


def include_trailing_semicolon_and_space(text: str, index: int) -> int:
    i = index
    while i < len(text) and text[i] in " \t":
        i += 1
    if i < len(text) and text[i] == ";":
        i += 1
    while i < len(text) and text[i] in " \t":
        i += 1
    if i < len(text) and text[i] == "\r":
        i += 1
    if i < len(text) and text[i] == "\n":
        i += 1
    return i


def extend_region_to_preceding_blank_line(text: str, start: int) -> int:
    line_start = text.rfind("\n", 0, start)
    if line_start < 0:
        return 0

    prev_line_end = line_start
    prev_line_start = text.rfind("\n", 0, prev_line_end - 1)
    prev_line_start = 0 if prev_line_start < 0 else prev_line_start + 1
    previous_line = text[prev_line_start:prev_line_end]
    if previous_line.strip() == "":
        return prev_line_start
    return line_start + 1


def merge_regions(regions: Iterable[MatchRegion]) -> list[MatchRegion]:
    ordered = sorted(regions, key=lambda item: (item.start, item.end))
    if not ordered:
        return []

    merged: list[MatchRegion] = [ordered[0]]
    for region in ordered[1:]:
        current = merged[-1]
        if region.start <= current.end:
            current.end = max(current.end, region.end)
            if region.reason != current.reason:
                current.reason = f"{current.reason}+{region.reason}"
            continue
        merged.append(region)
    return merged


def apply_regions(text: str, regions: list[MatchRegion]) -> str:
    if not regions:
        return text

    chunks: list[str] = []
    cursor = 0
    for region in regions:
        chunks.append(text[cursor:region.start])
        cursor = region.end
    chunks.append(text[cursor:])
    return "".join(chunks)


def normalize_blank_lines(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def remove_empty_preprocessor_guards(text: str) -> str:
    changed = True
    pattern = re.compile(
        r"(?ms)^[ \t]*#if[^\n]*\n(?:[ \t]*\n|[ \t]*//[^\n]*\n|[ \t]*/\*.*?\*/[ \t]*\n)*[ \t]*#endif[^\n]*\n?"
    )
    while changed:
        text, count = pattern.subn("", text)
        changed = count > 0
    return text


def unified_diff(path: Path, before: str, after: str, context: int) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=str(path),
        tofile=str(path),
        n=context,
    )
    return "".join(diff)


def build_file_plan(path: Path, args: argparse.Namespace, literal_specs: list[str]) -> FilePlan | None:
    original_text = read_text(path, args.encoding)
    regions: list[MatchRegion] = []

    for snippet in literal_specs:
        found = find_literal_regions(original_text, snippet, args.all_matches)
        if not found and not args.ignore_missing:
            raise SystemExit(f"literal selector not found in {path}: {snippet[:80]!r}")
        regions.extend(found)

    for pattern in args.regex:
        found = find_regex_regions(original_text, pattern, args.all_matches)
        if not found and not args.ignore_missing:
            raise SystemExit(f"regex selector not found in {path}: {pattern!r}")
        regions.extend(found)

    for value in args.line_range:
        start_line, end_line = parse_line_range(value)
        regions.append(find_line_range_region(original_text, start_line, end_line))

    for start_marker, end_marker in [*args.between, *args.marker_block]:
        found = find_between_regions(original_text, start_marker, end_marker, args.all_matches)
        if not found and not args.ignore_missing:
            raise SystemExit(
                f"between selector not found in {path}: {start_marker!r} -> {end_marker!r}"
            )
        regions.extend(found)

    for symbol in args.symbol:
        found = find_symbol_regions(original_text, symbol, args.all_matches)
        if not found and not args.ignore_missing:
            raise SystemExit(f"symbol selector not found in {path}: {symbol!r}")
        regions.extend(found)

    for needle in args.include_line:
        found = find_include_regions(original_text, needle, args.all_matches)
        if not found and not args.ignore_missing:
            raise SystemExit(f"include-line selector not found in {path}: {needle!r}")
        regions.extend(found)

    merged = merge_regions(regions)
    updated_text = apply_regions(original_text, merged)

    if args.trim_blank_lines:
        updated_text = normalize_blank_lines(updated_text)

    if args.empty_preprocessor_guard:
        updated_text = remove_empty_preprocessor_guards(updated_text)

    if updated_text == original_text:
        return None

    return FilePlan(
        path=path,
        original_text=original_text,
        updated_text=updated_text,
        removals=merged,
    )


def validate_expectations(plans: list[FilePlan], args: argparse.Namespace) -> None:
    if args.fail_on_noop and not plans:
        raise SystemExit("no changes would be made")

    if args.expect_per_file is not None:
        for plan in plans:
            if len(plan.removals) != args.expect_per_file:
                raise SystemExit(
                    f"{plan.path} has {len(plan.removals)} removals, expected {args.expect_per_file}"
                )

    if args.expect_total is not None:
        total = sum(len(plan.removals) for plan in plans)
        if total != args.expect_total:
            raise SystemExit(f"total removals {total}, expected {args.expect_total}")


def print_preview(plans: list[FilePlan], args: argparse.Namespace) -> None:
    if not plans:
        print("No changes.")
        return

    for index, plan in enumerate(plans):
        if index:
            print("\n" + ("=" * 80) + "\n")
        print(
            f"# {plan.path} | removals={len(plan.removals)} | "
            f"bytes={len(plan.original_text) - len(plan.updated_text)}"
        )
        print(unified_diff(plan.path, plan.original_text, plan.updated_text, args.context))


def apply_changes(plans: list[FilePlan], args: argparse.Namespace) -> None:
    for plan in plans:
        if args.backup:
            backup_path = plan.path.with_suffix(plan.path.suffix + ".bak")
            shutil.copyfile(plan.path, backup_path)
        write_text(plan.path, plan.updated_text, args.encoding)


def emit_json_report(plans: list[FilePlan]) -> None:
    report = {
        "changed_files": len(plans),
        "total_removals": sum(len(plan.removals) for plan in plans),
        "files": [
            {
                "path": str(plan.path),
                "removals": len(plan.removals),
                "bytes_removed": len(plan.original_text) - len(plan.updated_text),
                "reasons": [region.reason for region in plan.removals],
            }
            for plan in plans
        ],
    }
    print(json.dumps(report, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    target_paths = collect_target_paths(args)
    literal_specs = load_literal_specs(args)

    plans: list[FilePlan] = []
    for path in target_paths:
        plan = build_file_plan(path, args, literal_specs)
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
