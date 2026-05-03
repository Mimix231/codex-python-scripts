#!/usr/bin/env python3
"""
execution_subagent.py

Run a bounded execution/validation packet so Abyss can offload command
execution and summarize the resulting failures or successes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run execution commands for Abyss.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root.")
    parser.add_argument("--command", action="append", default=[], help="Command to run. May be repeated.")
    parser.add_argument("--timeout", type=int, default=180, help="Per-command timeout in seconds. Default: 180")
    parser.add_argument("--stop-on-failure", action="store_true", help="Stop after the first failing command.")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def run_command(command: str, cwd: Path, timeout: int) -> dict[str, object]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return {
            "command": command,
            "exitCode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "passed": completed.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "exitCode": -1,
            "stdout": "",
            "stderr": f"Timed out after {timeout} seconds.",
            "passed": False,
        }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    if not args.command:
        raise SystemExit("at least one --command is required")

    results = []
    failures = []
    for command in args.command:
        result = run_command(command, root, args.timeout)
        results.append(result)
        if not result["passed"]:
          failures.append(result)
          if args.stop_on_failure:
              break

    print(f"Execution subagent ran {len(results)} command(s).")
    for result in results:
        print(f"- [{'PASS' if result['passed'] else 'FAIL'}] {result['command']}")

    if args.json_report:
        print(
            json.dumps(
                {
                    "root": root.as_posix(),
                    "commands": args.command,
                    "results": results,
                    "failures": failures,
                    "passed": len(failures) == 0,
                },
                indent=2,
            )
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
