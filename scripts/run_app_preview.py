#!/usr/bin/env python3
"""
run_app_preview.py

Start, inspect, or stop a local app preview process so Abyss can verify that a
project actually launches.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

RUNTIME_DIR = Path(".glitch") / "runtime"
METADATA_PATH = RUNTIME_DIR / "app_preview.json"
LOG_PATH = RUNTIME_DIR / "app_preview.log"
URL_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d+[\w\-./?=&%]*", re.IGNORECASE)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start, query, or stop a local app preview process."
    )
    parser.add_argument("--workspace-root", default=".", help="Workspace root. Default: current directory")
    parser.add_argument("--command", help="Explicit preview command. If omitted, a command is inferred.")
    parser.add_argument("--wait-seconds", type=float, default=20.0, help="How long to wait for a URL. Default: 20")
    parser.add_argument("--status", action="store_true", help="Read the current preview status.")
    parser.add_argument("--stop", action="store_true", help="Stop the tracked preview process.")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def detect_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def infer_command(root: Path) -> str | None:
    package_json = root / "package.json"
    if package_json.exists():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        scripts = payload.get("scripts", {}) if isinstance(payload, dict) else {}
        manager = detect_package_manager(root)
        for script_name in ["dev", "start", "preview"]:
            if script_name in scripts:
                return f"{manager} run {script_name}"
    for candidate in ["main.py", "app.py"]:
        if (root / candidate).exists():
            return f'"{sys.executable}" {candidate}'
    return None


def ensure_runtime_dir(root: Path) -> None:
    (root / RUNTIME_DIR).mkdir(parents=True, exist_ok=True)


def metadata_file(root: Path) -> Path:
    return root / METADATA_PATH


def log_file(root: Path) -> Path:
    return root / LOG_PATH


def read_metadata(root: Path) -> dict[str, object] | None:
    path = metadata_file(root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_metadata(root: Path, payload: dict[str, object]) -> None:
    ensure_runtime_dir(root)
    metadata_file(root).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def start_preview(root: Path, command: str, wait_seconds: float) -> dict[str, object]:
    ensure_runtime_dir(root)
    log_path = log_file(root)
    with log_path.open("w", encoding="utf-8") as log_handle:
        creationflags = 0
        kwargs: dict[str, object] = {
            "cwd": root,
            "shell": True,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "text": True,
        }
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)

    detected_url = None
    deadline = time.time() + max(1.0, wait_seconds)
    while time.time() < deadline:
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8", errors="replace")
            match = URL_RE.search(content)
            if match:
                detected_url = match.group(0)
                break
        if process.poll() is not None:
            break
        time.sleep(0.5)

    payload = {
        "pid": process.pid,
        "command": command,
        "logPath": str(log_path.resolve()),
        "url": detected_url,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "running": process.poll() is None,
    }
    write_metadata(root, payload)
    return payload


def stop_preview(root: Path) -> dict[str, object]:
    payload = read_metadata(root)
    if not payload:
        return {"stopped": False, "reason": "No preview metadata found."}
    pid = int(payload.get("pid", -1))
    if pid <= 0:
        return {"stopped": False, "reason": "Invalid preview pid."}

    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    updated = dict(payload)
    updated["running"] = False
    updated["stoppedAt"] = datetime.now(timezone.utc).isoformat()
    write_metadata(root, updated)
    return {"stopped": True, "pid": pid}


def current_status(root: Path) -> dict[str, object]:
    payload = read_metadata(root)
    if not payload:
        return {"running": False, "reason": "No preview metadata found."}
    pid = int(payload.get("pid", -1))
    payload = dict(payload)
    payload["running"] = pid > 0 and is_process_alive(pid)
    return payload


def emit(payload: dict[str, object], json_report: bool) -> int:
    print(json.dumps(payload, indent=2) if json_report else summarize(payload))
    return 0 if payload.get("running") or payload.get("stopped") or payload.get("url") or payload.get("reason") else 1


def summarize(payload: dict[str, object]) -> str:
    if payload.get("stopped"):
        return f"Stopped preview process {payload.get('pid')}"
    if payload.get("reason") and not payload.get("running"):
        return str(payload["reason"])
    if payload.get("running"):
        url = payload.get("url") or "URL not detected yet"
        return f"Preview running (pid {payload.get('pid')}): {url}"
    return "Preview is not running."


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    if args.status:
        return emit(current_status(root), args.json_report)
    if args.stop:
        return emit(stop_preview(root), args.json_report)

    command = args.command or infer_command(root)
    if not command:
        raise SystemExit("No preview command was provided and none could be inferred.")
    return emit(start_preview(root, command, args.wait_seconds), args.json_report)


if __name__ == "__main__":
    raise SystemExit(main())
