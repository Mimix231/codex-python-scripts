#!/usr/bin/env python3
"""
get_errors.py

Run stack-aware diagnostics and return only the concrete failing checks so
Abyss can review or repair the workspace from real errors.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect structured workspace errors.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root.")
    parser.add_argument("--command", action="append", default=[], help="Explicit command to run. May be repeated.")
    parser.add_argument("--timeout", type=int, default=180, help="Per-command timeout in seconds. Default: 180")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def detect_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def derive_commands(root: Path) -> list[str]:
    commands: list[str] = []
    package_json = root / "package.json"
    if package_json.exists():
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            package_data = {}
        scripts = package_data.get("scripts", {}) if isinstance(package_data, dict) else {}
        manager = detect_package_manager(root)
        for script_name in ["typecheck", "lint", "build", "test"]:
            if script_name in scripts:
                commands.append(f"{manager} run {script_name}")
    if (root / "Cargo.toml").exists():
        commands.append("cargo check")
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists() or list(root.glob("*.py")):
        commands.append("python -m compileall .")
        if (root / "tests").exists():
            commands.append("python -m pytest")

    deduped: list[str] = []
    seen = set()
    for command in commands:
        if command in seen:
            continue
        seen.add(command)
        deduped.append(command)
    return deduped


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

    commands = args.command or derive_commands(root)
    failures = []
    for command in commands:
        result = run_command(command, root, args.timeout)
        if not result["passed"]:
            failures.append(result)

    print(f"Collected {len(failures)} failing diagnostic command(s).")
    for failure in failures:
        print(f"- {failure['command']}")

    if args.json_report:
        print(
            json.dumps(
                {
                    "root": root.as_posix(),
                    "commands": commands,
                    "failures": failures,
                    "count": len(failures),
                    "passed": len(failures) == 0,
                },
                indent=2,
            )
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
