#!/usr/bin/env python3
"""
continue_task.py

Build the next bounded continuation packet when a coding task is too large for
one model pass or needs to resume from a checkpoint.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a deterministic continuation packet for the next coding pass."
    )
    parser.add_argument("--prompt", required=True, help="Original user request.")
    parser.add_argument("--checkpoint-summary", default="", help="Summary of the latest checkpoint.")
    parser.add_argument("--next-goal", default="", help="Optional explicit next goal.")
    parser.add_argument("--remaining-work", action="append", default=[], help="Remaining work item. May be repeated.")
    parser.add_argument("--modified-file", action="append", default=[], help="Modified file path. May be repeated.")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def build_report(args: argparse.Namespace) -> dict[str, object]:
    remaining = [item.strip() for item in args.remaining_work if item.strip()]
    next_goal = args.next_goal.strip() or (remaining[0] if remaining else "Continue from the current workspace state.")
    suggested_prompt = "\n\n".join(
        part for part in [
            f"Original request:\n{args.prompt.strip()}",
            f"Latest checkpoint summary:\n{args.checkpoint_summary.strip()}" if args.checkpoint_summary.strip() else "",
            f"Next goal:\n{next_goal}",
            "Remaining work:\n" + "\n".join(f"- {item}" for item in remaining) if remaining else "",
            "Modified files to re-read:\n" + "\n".join(f"- {item}" for item in args.modified_file[:8]) if args.modified_file else "",
        ]
        if part
    )
    return {
        "nextGoal": next_goal,
        "remainingWork": remaining,
        "modifiedFiles": args.modified_file,
        "suggestedPrompt": suggested_prompt,
        "recommendedSteps": [
            "Refresh local context for the changed files.",
            "Complete the next bounded slice only.",
            "Run focused validation after writing code.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    report = build_report(args)

    print(f"Next goal: {report['nextGoal']}")
    if report["remainingWork"]:
        print("Remaining work:")
        for item in report["remainingWork"]:
            print(f"- {item}")

    if args.json_report:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
