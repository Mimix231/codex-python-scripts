#!/usr/bin/env python3
"""
build_guard.py

Run a build/test command, capture output, and summarize diagnostics in a more
structured way than a raw terminal dump.

Features:
- accepts a shell command or argv list from JSON
- captures stdout/stderr together in order
- parses common gcc/clang/msvc/python-style diagnostics
- prints grouped summaries with optional raw log output and JSON reports
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(slots=True)
class Diagnostic:
    file: str
    line: int | None
    column: int | None
    severity: str
    message: str
    raw: str


GCC_CLANG_RE = re.compile(
    r"^(?P<file>[^:(][^:]*)[:](?P<line>\d+)(?:[:](?P<col>\d+))?:\s*(?P<severity>fatal error|error|warning|note):\s*(?P<message>.*)$"
)
MSVC_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+)(?:,(?P<col>\d+))?\):\s*(?P<severity>fatal error|error|warning|note)\s+[A-Z]+\d+:\s*(?P<message>.*)$"
)
PYTHON_RE = re.compile(
    r'^\s*File\s+"(?P<file>.+?)",\s+line\s+(?P<line>\d+)(?:,\s+in\s+.*)?$'
)
RUST_RE = re.compile(
    r"^(?P<severity>error|warning)(?:\[[^\]]+\])?:\s*(?P<message>.*)$"
)
RUST_AT_RE = re.compile(r"^\s*-->\s+(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)$")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a build/test command and summarize diagnostics."
    )
    parser.add_argument(
        "--command",
        help="Command line to run via the shell.",
    )
    parser.add_argument(
        "--argv-json",
        help="JSON array argv to run without a shell.",
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="Working directory for the command. Default: current directory",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Environment override in KEY=VALUE form. May be repeated.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help="Timeout in seconds.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the raw combined output after the summary.",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=0,
        help="Print the last N lines of raw output after the summary.",
    )
    parser.add_argument(
        "--json-report",
        action="store_true",
        help="Emit a JSON report after the text summary.",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit non-zero if any warnings are detected.",
    )
    parser.add_argument(
        "--expect-exit",
        type=int,
        help="Expected process exit code.",
    )
    return parser.parse_args(argv)


def parse_env_overrides(entries: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in entries:
        if "=" not in raw:
            raise SystemExit(f"--env expects KEY=VALUE: {raw!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"--env expects a non-empty key: {raw!r}")
        env[key] = value
    return env


def resolve_command(args: argparse.Namespace) -> tuple[list[str] | str, bool]:
    has_shell = args.command is not None
    has_argv = args.argv_json is not None
    if has_shell == has_argv:
        raise SystemExit("provide exactly one of --command or --argv-json")
    if has_shell:
        return args.command, True
    payload = json.loads(args.argv_json)
    if not isinstance(payload, list) or not payload:
        raise SystemExit("--argv-json must be a non-empty JSON array")
    return [str(item) for item in payload], False


def run_command(args: argparse.Namespace) -> subprocess.CompletedProcess[str]:
    command, use_shell = resolve_command(args)
    env = os.environ.copy()
    env.update(parse_env_overrides(args.env))
    try:
        return subprocess.run(
            command,
            shell=use_shell,
            cwd=args.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"command timed out after {args.timeout} second(s)") from exc


def parse_diagnostics(output: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    pending_rust: tuple[str, str] | None = None
    pending_python: tuple[str, int] | None = None

    for line in output.splitlines():
        gcc = GCC_CLANG_RE.match(line)
        if gcc:
            diagnostics.append(
                Diagnostic(
                    file=gcc.group("file"),
                    line=int(gcc.group("line")) if gcc.group("line") else None,
                    column=int(gcc.group("col")) if gcc.group("col") else None,
                    severity=gcc.group("severity"),
                    message=gcc.group("message"),
                    raw=line,
                )
            )
            pending_rust = None
            pending_python = None
            continue

        msvc = MSVC_RE.match(line)
        if msvc:
            diagnostics.append(
                Diagnostic(
                    file=msvc.group("file"),
                    line=int(msvc.group("line")) if msvc.group("line") else None,
                    column=int(msvc.group("col")) if msvc.group("col") else None,
                    severity=msvc.group("severity"),
                    message=msvc.group("message"),
                    raw=line,
                )
            )
            pending_rust = None
            pending_python = None
            continue

        rust = RUST_RE.match(line)
        if rust:
            pending_rust = (rust.group("severity"), rust.group("message"))
            continue

        rust_at = RUST_AT_RE.match(line)
        if rust_at and pending_rust is not None:
            diagnostics.append(
                Diagnostic(
                    file=rust_at.group("file"),
                    line=int(rust_at.group("line")),
                    column=int(rust_at.group("col")),
                    severity=pending_rust[0],
                    message=pending_rust[1],
                    raw=f"{pending_rust[0]}: {pending_rust[1]} @ {line.strip()}",
                )
            )
            pending_rust = None
            continue

        py = PYTHON_RE.match(line)
        if py:
            pending_python = (py.group("file"), int(py.group("line")))
            continue
        if pending_python and line.strip() and not line.startswith(" "):
            diagnostics.append(
                Diagnostic(
                    file=pending_python[0],
                    line=pending_python[1],
                    column=None,
                    severity="error",
                    message=line.strip(),
                    raw=line,
                )
            )
            pending_python = None

    return diagnostics


def summarize_diagnostics(diagnostics: list[Diagnostic]) -> str:
    if not diagnostics:
        return "No parsed diagnostics."

    by_file: dict[str, list[Diagnostic]] = defaultdict(list)
    for diagnostic in diagnostics:
        by_file[diagnostic.file].append(diagnostic)

    lines: list[str] = []
    severity_counts: dict[str, int] = defaultdict(int)
    for diagnostic in diagnostics:
        severity_counts[diagnostic.severity] += 1

    counts_text = ", ".join(f"{severity}={count}" for severity, count in sorted(severity_counts.items()))
    lines.append(f"Parsed diagnostics: {counts_text}")
    for file_name in sorted(by_file):
        lines.append(f"- {file_name}: {len(by_file[file_name])}")
        for diagnostic in by_file[file_name][:10]:
            location = ""
            if diagnostic.line is not None:
                location = f":{diagnostic.line}"
                if diagnostic.column is not None:
                    location += f":{diagnostic.column}"
            lines.append(f"  {diagnostic.severity}{location}: {diagnostic.message}")
        if len(by_file[file_name]) > 10:
            lines.append(f"  ... {len(by_file[file_name]) - 10} more")
    return "\n".join(lines)


def should_fail(completed: subprocess.CompletedProcess[str], diagnostics: list[Diagnostic], args: argparse.Namespace) -> bool:
    if args.expect_exit is not None:
        if completed.returncode != args.expect_exit:
            return True
        if args.fail_on_warning and any(d.severity == "warning" for d in diagnostics):
            return True
        return False

    if completed.returncode != 0:
        return True
    if args.fail_on_warning and any(d.severity == "warning" for d in diagnostics):
        return True
    return False


def emit_json_report(completed: subprocess.CompletedProcess[str], diagnostics: list[Diagnostic]) -> None:
    payload = {
        "exit_code": completed.returncode,
        "diagnostics": [
            {
                "file": diagnostic.file,
                "line": diagnostic.line,
                "column": diagnostic.column,
                "severity": diagnostic.severity,
                "message": diagnostic.message,
            }
            for diagnostic in diagnostics
        ],
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    print(json.dumps(payload, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    completed = run_command(args)
    combined = completed.stdout + completed.stderr
    diagnostics = parse_diagnostics(combined)

    print(f"Command exit code: {completed.returncode}")
    print(summarize_diagnostics(diagnostics))

    if args.raw:
        print("\n# Raw output")
        print(combined, end="" if combined.endswith("\n") else "\n")
    elif args.tail > 0:
        print(f"\n# Last {args.tail} line(s)")
        lines = combined.splitlines()
        tail = lines[-args.tail:] if args.tail < len(lines) else lines
        if tail:
            print("\n".join(tail))

    if args.json_report:
        emit_json_report(completed, diagnostics)

    return 1 if should_fail(completed, diagnostics, args) else 0


if __name__ == "__main__":
    raise SystemExit(main())
