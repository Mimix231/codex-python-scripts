#!/usr/bin/env python3
"""
collect_project_context.py

Inspect the local workspace and assemble a compact, request-aware context
bundle that Abyss can feed into a coding pass.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

IGNORED_DIRS = {
    ".git",
    ".next",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "out",
    "target",
}
TEXT_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".css", ".go", ".h", ".hpp", ".html", ".ini",
    ".java", ".js", ".json", ".jsx", ".lua", ".md", ".mjs", ".py", ".rs",
    ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}
MANIFESTS = ["package.json", "pyproject.toml", "Cargo.toml", "requirements.txt", "README.md"]


@dataclass(slots=True)
class ContextFile:
    path: str
    reason: str
    score: int
    excerpt: str
    bytes: int


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect request-aware workspace context for Abyss."
    )
    parser.add_argument("--prompt", required=True, help="User request or prompt.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root. Default: current directory")
    parser.add_argument("--max-files", type=int, default=12, help="Maximum files to include. Default: 12")
    parser.add_argument(
        "--max-bytes-per-file",
        type=int,
        default=6000,
        help="Maximum bytes to read per selected file. Default: 6000",
    )
    parser.add_argument(
        "--max-total-bytes",
        type=int,
        default=50000,
        help="Maximum total bytes to include. Default: 50000",
    )
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def tokenize(text: str) -> list[str]:
    return [token for token in re.findall(r"[A-Za-z0-9_+-]+", text.lower()) if len(token) >= 2]


def should_scan(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS or path.name in MANIFESTS


def workspace_summary(root: Path) -> dict[str, object]:
    manifests = [name for name in MANIFESTS if (root / name).exists()]
    stacks = []
    if (root / "package.json").exists():
        stacks.append("node")
    if (root / "Cargo.toml").exists():
        stacks.append("rust")
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        stacks.append("python")
    if not stacks:
        stacks.append("unknown")
    return {
        "root": str(root.resolve()),
        "manifests": manifests,
        "stacks": stacks,
    }


def score_file(path: Path, root: Path, prompt_tokens: list[str]) -> tuple[int, str]:
    relative = path.relative_to(root).as_posix().lower()
    score = 0
    reasons: list[str] = []

    if path.name in MANIFESTS:
        score += 12
        reasons.append("project manifest")

    for token in prompt_tokens:
        if token in relative:
            score += 5
            reasons.append(f"path matches '{token}'")

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, ""

    content_lower = content.lower()
    hits = 0
    for token in prompt_tokens:
        if token in content_lower:
            hits += 1
            score += 2
    if hits:
        reasons.append(f"content matched {hits} request token(s)")

    if path.suffix.lower() in {".md", ".toml", ".json"}:
        score += 2

    return score, ", ".join(dict.fromkeys(reasons))


def collect_files(root: Path, prompt_tokens: list[str], max_bytes_per_file: int) -> list[ContextFile]:
    candidates: list[ContextFile] = []
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if name not in IGNORED_DIRS]
        for file_name in file_names:
            path = Path(current_root) / file_name
            if not should_scan(path):
                continue
            score, reason = score_file(path, root, prompt_tokens)
            if score <= 0:
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            excerpt = raw[:max_bytes_per_file].strip()
            candidates.append(
                ContextFile(
                    path=path.relative_to(root).as_posix(),
                    reason=reason or "relevant project file",
                    score=score,
                    excerpt=excerpt,
                    bytes=min(len(raw.encode("utf-8", errors="ignore")), max_bytes_per_file),
                )
            )
    candidates.sort(key=lambda item: (-item.score, item.path))
    return candidates


def select_files(candidates: list[ContextFile], max_files: int, max_total_bytes: int) -> list[ContextFile]:
    selected: list[ContextFile] = []
    total = 0
    for candidate in candidates:
        if len(selected) >= max_files:
            break
        if total + candidate.bytes > max_total_bytes and selected:
            continue
        selected.append(candidate)
        total += candidate.bytes
    return selected


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    tokens = tokenize(args.prompt)
    candidates = collect_files(root, tokens, max(500, args.max_bytes_per_file))
    selected = select_files(candidates, max(1, args.max_files), max(2000, args.max_total_bytes))
    summary = workspace_summary(root)
    report = {
        **summary,
        "prompt": args.prompt,
        "selectedCount": len(selected),
        "files": [asdict(item) for item in selected],
    }

    print(f"Collected {len(selected)} context file(s) from {root.name}.")
    for item in selected:
        print(f"- {item.path} ({item.reason})")

    if args.json_report:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
