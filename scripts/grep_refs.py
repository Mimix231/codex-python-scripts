#!/usr/bin/env python3
"""
grep_refs.py

Structured search helper for literals, regexes, symbols, and include/import
references across one or many files.

Features:
- target files via explicit list, globs, or file lists
- literal, regex, symbol-boundary, include, and import modes
- grouped text output or JSON output
- optional context lines and max-match limiting
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_ENCODING = "utf-8"


@dataclass(slots=True)
class Match:
    path: Path
    line: int
    column: int
    text: str
    context_before: list[str]
    context_after: list[str]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search references across files and report grouped matches."
    )
    parser.add_argument("--files", nargs="+", default=[], help="Explicit target files.")
    parser.add_argument("--glob", action="append", default=[], help="Glob pattern expanded from the current working directory.")
    parser.add_argument("--file-list", action="append", default=[], help="Text file containing one target path per line.")
    parser.add_argument("--exclude-glob", action="append", default=[], help="Glob pattern to exclude from targets.")

    parser.add_argument("--find", help="Literal text to search for.")
    parser.add_argument("--regex", help="Regex pattern to search for.")
    parser.add_argument("--symbol", help="Identifier-boundary symbol to search for.")
    parser.add_argument("--include", help="C-like include token to search for.")
    parser.add_argument("--import", dest="import_name", help="Python import token to search for.")

    parser.add_argument("--ignore-case", action="store_true", help="Case-insensitive matching.")
    parser.add_argument("--multiline", action="store_true", help="Regex multiline mode.")
    parser.add_argument("--dotall", action="store_true", help="Regex dotall mode.")
    parser.add_argument("--context", type=int, default=0, help="Context lines before/after each match.")
    parser.add_argument("--max-matches", type=int, help="Maximum total matches to emit.")
    parser.add_argument("--count-only", action="store_true", help="Only print counts per file.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text output.")
    parser.add_argument("--encoding", default=DEFAULT_ENCODING, help=f"Text encoding. Default: {DEFAULT_ENCODING}")
    return parser.parse_args(argv)


def read_text(path: Path, encoding: str) -> str:
    try:
        return path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        raise SystemExit(f"could not decode file with {encoding}: {path}")
    except FileNotFoundError:
        raise SystemExit(f"file not found: {path}")


def collect_targets(args: argparse.Namespace) -> list[Path]:
    ordered: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path in seen:
            return
        seen.add(path)
        ordered.append(path)

    for raw in args.files:
        add(Path(raw))

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

    if not ordered:
        raise SystemExit("no target files specified")
    return ordered


def compile_pattern(args: argparse.Namespace) -> re.Pattern[str]:
    specified = sum(
        1
        for value in [
            args.find is not None,
            args.regex is not None,
            args.symbol is not None,
            args.include is not None,
            args.import_name is not None,
        ]
        if value
    )
    if specified != 1:
        raise SystemExit("provide exactly one of --find, --regex, --symbol, --include, or --import")

    flags = 0
    if args.ignore_case:
        flags |= re.IGNORECASE
    if args.multiline:
        flags |= re.MULTILINE
    if args.dotall:
        flags |= re.DOTALL

    if args.find is not None:
        pattern = re.escape(args.find)
    elif args.regex is not None:
        pattern = args.regex
    elif args.symbol is not None:
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(args.symbol)}(?![A-Za-z0-9_])"
    elif args.include is not None:
        token = re.escape(args.include)
        pattern = rf"^\s*#include\s+(?:<|\")?{token}(?:>|\")?\s*$"
        flags |= re.MULTILINE
    else:
        token = re.escape(args.import_name)
        pattern = rf"^\s*(?:from\s+{token}\b|import\s+{token}\b)"
        flags |= re.MULTILINE

    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise SystemExit(f"invalid pattern {pattern!r}: {exc}") from exc


def line_starts(text: str) -> list[int]:
    starts = [0]
    for match in re.finditer(r"\n", text):
        starts.append(match.end())
    return starts


def offset_to_line_col(starts: list[int], offset: int) -> tuple[int, int]:
    low = 0
    high = len(starts) - 1
    while low <= high:
        mid = (low + high) // 2
        if starts[mid] <= offset:
            low = mid + 1
        else:
            high = mid - 1
    line_index = high
    line_number = line_index + 1
    column = offset - starts[line_index] + 1
    return line_number, column


def find_matches(path: Path, regex: re.Pattern[str], args: argparse.Namespace) -> list[Match]:
    text = read_text(path, args.encoding)
    starts = line_starts(text)
    lines = text.splitlines()
    results: list[Match] = []

    for match in regex.finditer(text):
        line_number, column = offset_to_line_col(starts, match.start())
        line_text = lines[line_number - 1] if 0 <= line_number - 1 < len(lines) else ""
        before_start = max(0, line_number - 1 - args.context)
        after_end = min(len(lines), line_number + args.context)
        results.append(
            Match(
                path=path,
                line=line_number,
                column=column,
                text=line_text,
                context_before=lines[before_start: line_number - 1],
                context_after=lines[line_number: after_end],
            )
        )
        if args.max_matches is not None and len(results) >= args.max_matches:
            break
    return results


def collect_all_matches(targets: list[Path], regex: re.Pattern[str], args: argparse.Namespace) -> dict[Path, list[Match]]:
    results: dict[Path, list[Match]] = {}
    total = 0
    for path in targets:
        file_matches = find_matches(path, regex, args)
        if file_matches:
            remaining = None if args.max_matches is None else args.max_matches - total
            if remaining is not None and remaining <= 0:
                break
            if remaining is not None:
                file_matches = file_matches[:remaining]
            results[path] = file_matches
            total += len(file_matches)
            if args.max_matches is not None and total >= args.max_matches:
                break
    return results


def print_text(matches_by_file: dict[Path, list[Match]], args: argparse.Namespace) -> None:
    if not matches_by_file:
        print("No matches.")
        return

    for index, (path, matches) in enumerate(matches_by_file.items()):
        if index:
            print("\n" + ("=" * 80) + "\n")
        if args.count_only:
            print(f"{path}: {len(matches)}")
            continue
        print(f"# {path} | matches={len(matches)}")
        for match in matches:
            print(f"- {match.line}:{match.column}: {match.text}")
            for ctx in match.context_before:
                print(f"    {ctx}")
            for ctx in match.context_after:
                print(f"    {ctx}")


def print_json(matches_by_file: dict[Path, list[Match]]) -> None:
    payload = {
        "files": [
            {
                "path": str(path),
                "match_count": len(matches),
                "matches": [
                    {
                        "line": match.line,
                        "column": match.column,
                        "text": match.text,
                        "context_before": match.context_before,
                        "context_after": match.context_after,
                    }
                    for match in matches
                ],
            }
            for path, matches in matches_by_file.items()
        ]
    }
    print(json.dumps(payload, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    targets = collect_targets(args)
    regex = compile_pattern(args)
    matches_by_file = collect_all_matches(targets, regex, args)
    if args.json:
        print_json(matches_by_file)
    else:
        print_text(matches_by_file, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
