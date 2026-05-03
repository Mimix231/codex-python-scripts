#!/usr/bin/env python3
"""
update_decision_memory.py

Extract durable project decisions from the latest request/summary pair so Abyss
can carry forward stack, architecture, style, and workflow constraints without
replaying the whole transcript.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Sequence


STACK_KEYWORDS = {
    "react": "Use React in the frontend application layer",
    "typescript": "Use TypeScript for the JavaScript-side codebase",
    "javascript": "Use JavaScript where TypeScript is not required",
    "tauri": "Use Tauri for the desktop shell and command bridge",
    "rust": "Use Rust for backend/native command execution",
    "vite": "Use Vite as the frontend bundler and dev runtime",
    "tailwind": "Use Tailwind CSS for styling",
    "shadcn": "Use shadcn/ui-style primitives for UI composition",
    "node": "Use Node-based tooling and package management",
    "python": "Use Python for ML or auxiliary runtime tasks",
}

LIBRARY_KEYWORDS = {
    "zustand": "Use Zustand for client-side state management",
    "framer motion": "Use Framer Motion for animation and transitions",
    "lucide": "Use Lucide icons for interface iconography",
    "monaco": "Use Monaco Editor for code or artifact editing surfaces",
    "ollama": "Use Ollama-compatible local model serving",
    "llama.cpp": "Use llama.cpp-compatible local inference runtime",
}

STYLE_KEYWORDS = {
    "premium": "Target a premium, polished interface instead of raw scaffolding",
    "modern": "Prefer modern visual language and spacing",
    "retro": "Keep the visual style retro without making it feel unfinished",
    "minimal": "Keep the UI minimal and focused",
    "editor": "Favor an editor/workbench layout with strong information hierarchy",
}

ARCHITECTURE_PATTERNS = [
    (re.compile(r"\bsidebar\b", re.IGNORECASE), "Include a sidebar/navigation region in the app shell"),
    (re.compile(r"\bsettings?\b", re.IGNORECASE), "Expose a settings surface in the application shell"),
    (re.compile(r"\beditor\b", re.IGNORECASE), "Treat the editor surface as a first-class module"),
    (re.compile(r"\bchat\b", re.IGNORECASE), "Treat the chat surface as a first-class interaction area"),
    (re.compile(r"\bcomponent\b", re.IGNORECASE), "Organize implementation around reusable components"),
    (re.compile(r"\bservice\b", re.IGNORECASE), "Separate service/runtime logic from UI composition"),
    (re.compile(r"\bplugin\b", re.IGNORECASE), "Keep plugin/tool integrations as explicit runtime capabilities"),
]

REJECTED_PATTERNS = [
    (re.compile(r"\bdon['’]t use\b\s+([a-z0-9_.-]+)", re.IGNORECASE), "Avoid using {value}"),
    (re.compile(r"\bno\b\s+([a-z0-9_.-]+)\s+(?:here|please|anymore)?", re.IGNORECASE), "Reject approach: {value}"),
]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract durable decision-memory entries from the latest run.")
    parser.add_argument("--request", required=True, help="Raw user request.")
    parser.add_argument("--assistant-summary", required=True, help="Assistant summary or final result.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root for relative path handling.")
    parser.add_argument("--modified-files", nargs="*", default=[], help="Files touched by the run.")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON report.")
    return parser.parse_args(argv)


def normalize_modified_files(root: Path, values: Iterable[str]) -> list[str]:
    files: list[str] = []
    for value in values:
      if not value:
        continue
      path = Path(value)
      try:
        if path.is_absolute():
          files.append(path.relative_to(root).as_posix())
        else:
          files.append(path.as_posix())
      except ValueError:
        files.append(path.as_posix())
    return list(dict.fromkeys(files))[:16]


def add_entry(entries: list[dict[str, object]], kind: str, summary: str, *, related_files: list[str], tags: list[str], evidence: str | None = None):
    summary = summary.strip()
    if not summary:
        return
    payload = {
        "kind": kind,
        "summary": summary,
        "relatedFiles": list(dict.fromkeys(related_files))[:8],
        "tags": list(dict.fromkeys([tag.strip().lower() for tag in tags if tag and tag.strip()]))[:12],
    }
    if evidence:
        payload["evidence"] = evidence.strip()
    if payload not in entries:
        entries.append(payload)


def extract_keyword_entries(text: str, entries: list[dict[str, object]], modified_files: list[str]):
    lowered = text.lower()
    for keyword, summary in STACK_KEYWORDS.items():
        if keyword in lowered:
            add_entry(entries, "stack", summary, related_files=modified_files, tags=[keyword])
    for keyword, summary in LIBRARY_KEYWORDS.items():
        if keyword in lowered:
            add_entry(entries, "library", summary, related_files=modified_files, tags=keyword.split())
    for keyword, summary in STYLE_KEYWORDS.items():
        if keyword in lowered:
            add_entry(entries, "style", summary, related_files=modified_files, tags=[keyword])
    for pattern, summary in ARCHITECTURE_PATTERNS:
        if pattern.search(text):
            add_entry(entries, "architecture", summary, related_files=modified_files, tags=pattern.pattern.split("\\b")[1:2] or ["architecture"])


def extract_file_based_entries(entries: list[dict[str, object]], modified_files: list[str]):
    if any(path.endswith("package.json") for path in modified_files):
        add_entry(
            entries,
            "workflow",
            "Keep package.json scripts and Node tooling aligned with the requested app workflow",
            related_files=[path for path in modified_files if path.endswith("package.json")],
            tags=["package.json", "node", "scripts"],
        )
    if any(path.endswith("Cargo.toml") or path.endswith(".rs") for path in modified_files):
        add_entry(
            entries,
            "architecture",
            "Rust-side files define part of the backend/native execution surface",
            related_files=[path for path in modified_files if path.endswith("Cargo.toml") or path.endswith(".rs")],
            tags=["rust", "backend", "tauri"],
        )
    if any(path.endswith((".tsx", ".ts", ".jsx", ".js")) for path in modified_files):
        add_entry(
            entries,
            "architecture",
            "Frontend application behavior is implemented through the TypeScript/JavaScript client surface",
            related_files=[path for path in modified_files if path.endswith((".tsx", ".ts", ".jsx", ".js"))],
            tags=["frontend", "client", "ui"],
        )


def extract_rejections(text: str, entries: list[dict[str, object]], modified_files: list[str]):
    for pattern, template in REJECTED_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1)
            add_entry(
                entries,
                "rejected_approach",
                template.format(value=value),
                related_files=modified_files,
                tags=[value, "avoid"],
                evidence=match.group(0),
            )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    modified_files = normalize_modified_files(root, args.modified_files)
    request = args.request.strip()
    assistant_summary = args.assistant_summary.strip()
    combined = f"{request}\n{assistant_summary}"
    entries: list[dict[str, object]] = []

    extract_keyword_entries(combined, entries, modified_files)
    extract_file_based_entries(entries, modified_files)
    extract_rejections(request, entries, modified_files)

    if "exactly like" in request.lower() or "behave like" in request.lower():
        add_entry(
            entries,
            "constraint",
            "Match the referenced behavior closely instead of inventing a new interaction model",
            related_files=modified_files,
            tags=["reference", "behavior", "constraint"],
            evidence=request,
        )
    if "one pass" in request.lower():
        add_entry(
            entries,
            "workflow",
            "Prefer completing the requested slice in one pass whenever the runtime allows it",
            related_files=modified_files,
            tags=["one-pass", "workflow"],
            evidence=request,
        )

    report = {
        "entries": entries,
        "count": len(entries),
    }

    print(f"Extracted {len(entries)} decision-memory entr{'y' if len(entries) == 1 else 'ies'}.")
    if args.json_report:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
