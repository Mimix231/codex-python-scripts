#!/usr/bin/env python3
"""
js_code_sandbox.py

Run a bounded JavaScript or TypeScript snippet in an isolated sandbox under
.glitch/sandbox and remove the sandbox directory after execution.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Sequence

SANDBOX_ROOT = Path(".glitch") / "sandbox"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a JS/TS snippet in a local sandbox.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root.")
    parser.add_argument("--language", choices=["javascript", "typescript"], default="javascript")
    parser.add_argument("--timeout", type=int, default=45, help="Timeout in seconds. Default: 45")
    parser.add_argument("--filename", help="Optional sandbox filename.")
    parser.add_argument("--stdin", action="store_true", help="Read code from stdin.")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def read_code(args: argparse.Namespace) -> str:
    if not args.stdin:
        raise SystemExit("js_code_sandbox.py requires --stdin.")
    return sys.stdin.read()


def resolve_command(root: Path, language: str, filename: str) -> list[str]:
    if language == "typescript":
        local_tsx = root / "node_modules" / ".bin" / ("tsx.cmd" if sys.platform.startswith("win") else "tsx")
        if local_tsx.exists():
          return [str(local_tsx), filename]
        return ["npx", "tsx", filename]
    return ["node", filename]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    code = read_code(args)
    extension = "ts" if args.language == "typescript" else "js"
    sandbox_id = uuid.uuid4().hex[:12]
    sandbox_dir = root / SANDBOX_ROOT / sandbox_id
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    filename = args.filename or f"snippet.{extension}"
    file_path = sandbox_dir / filename
    file_path.write_text(code, encoding="utf-8")

    command = resolve_command(root, args.language, filename)
    try:
        completed = subprocess.run(
            command,
            cwd=sandbox_dir,
            text=True,
            capture_output=True,
            timeout=args.timeout,
        )
        report = {
            "root": root.as_posix(),
            "language": args.language,
            "command": " ".join(command),
            "exitCode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "passed": completed.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        report = {
            "root": root.as_posix(),
            "language": args.language,
            "command": " ".join(command),
            "exitCode": -1,
            "stdout": "",
            "stderr": f"Timed out after {args.timeout} seconds.",
            "passed": False,
        }
    finally:
        shutil.rmtree(sandbox_dir, ignore_errors=True)

    status = "PASS" if report["passed"] else "FAIL"
    print(f"JS sandbox {status}: {report['command']}")
    if args.json_report:
        print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
