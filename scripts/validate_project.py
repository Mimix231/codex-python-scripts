#!/usr/bin/env python3
"""
validate_project.py

Run stack-aware validation commands and return structured pass/fail results so
Abyss can repair the workspace based on concrete failures.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run stack-aware validation commands in the current workspace."
    )
    parser.add_argument("--workspace-root", default=".", help="Workspace root. Default: current directory")
    parser.add_argument("--command", action="append", default=[], help="Explicit command to run. May be repeated.")
    parser.add_argument(
        "--include-install",
        action="store_true",
        help="Include package install commands when they can be inferred.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Per-command timeout in seconds. Default: 180",
    )
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def detect_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def package_manager_install_command(manager: str) -> str:
    if manager == "pnpm":
        return "pnpm install"
    if manager == "yarn":
        return "yarn install"
    return "npm install"


def derive_commands(root: Path, include_install: bool) -> list[str]:
    commands: list[str] = []

    package_json = root / "package.json"
    if package_json.exists():
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            package_data = {}
        scripts = package_data.get("scripts", {}) if isinstance(package_data, dict) else {}
        manager = detect_package_manager(root)
        if include_install:
            commands.append(package_manager_install_command(manager))
        for script_name in ["typecheck", "lint", "build", "test"]:
            if script_name in scripts:
                commands.append(f"{manager} run {script_name}")

    if (root / "Cargo.toml").exists():
        commands.append("cargo check")
        commands.append("cargo test --no-run")

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


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    commands = args.command or derive_commands(root, args.include_install)
    if not commands:
        print("No validation commands were selected.")
        if args.json_report:
            print(json.dumps({"commands": [], "results": [], "failures": []}, indent=2))
        return 0

    results = []
    failures = []
    for command in commands:
        try:
            result = run_command(command, root, args.timeout)
        except subprocess.TimeoutExpired:
            result = {
                "command": command,
                "exitCode": -1,
                "stdout": "",
                "stderr": f"Timed out after {args.timeout} seconds.",
                "passed": False,
            }
        results.append(result)
        if not result["passed"]:
            failures.append(result)

    print(f"Validation commands run: {len(results)}")
    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {result['command']}")

    if args.json_report:
        print(json.dumps({
            "commands": commands,
            "results": results,
            "failures": failures,
            "passed": len(failures) == 0,
        }, indent=2))

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
