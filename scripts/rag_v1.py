#!/usr/bin/env python3
"""
rag_v1.py

Build a lightweight workspace retrieval index under .glitch/index and return
the most relevant local chunks for a query. This is lexical and deterministic,
intended as a built-in local project RAG layer for Abyss.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
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
INDEX_DIR = Path(".glitch") / "index"
INDEX_PATH = INDEX_DIR / "rag-v1.json"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query the built-in project RAG index.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root.")
    parser.add_argument("--query", required=True, help="Retrieval query.")
    parser.add_argument("--max-files", type=int, default=10, help="Maximum distinct files. Default: 10")
    parser.add_argument("--max-chunks", type=int, default=8, help="Maximum chunks to return. Default: 8")
    parser.add_argument("--chunk-chars", type=int, default=1200, help="Approximate chunk size. Default: 1200")
    parser.add_argument("--reindex", action="store_true", help="Force a fresh index rebuild.")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON after the summary.")
    return parser.parse_args(argv)


def tokenize(text: str) -> list[str]:
    return [token for token in re.findall(r"[A-Za-z0-9_+-]+", text.lower()) if len(token) >= 2]


def should_index(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS


def split_chunks(content: str, chunk_chars: int) -> list[tuple[str, int, int]]:
    lines = content.splitlines()
    chunks: list[tuple[str, int, int]] = []
    current: list[str] = []
    start_line = 1
    current_length = 0

    for index, line in enumerate(lines, start=1):
        if not current:
            start_line = index
        current.append(line)
        current_length += len(line) + 1
        if current_length >= chunk_chars:
            chunks.append(("\n".join(current).strip(), start_line, index))
            current = []
            current_length = 0

    if current:
        chunks.append(("\n".join(current).strip(), start_line, len(lines)))
    return [(text, start, end) for text, start, end in chunks if text.strip()]


def build_index(root: Path, chunk_chars: int) -> tuple[list[dict[str, object]], int]:
    chunks: list[dict[str, object]] = []
    indexed_files = 0
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if name not in IGNORED_DIRS]
        for file_name in file_names:
            path = Path(current_root) / file_name
            if not should_index(path):
                continue
            try:
              content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
              continue
            indexed_files += 1
            relative = path.relative_to(root).as_posix()
            for chunk_text, start_line, end_line in split_chunks(content, chunk_chars):
                chunks.append(
                    {
                        "path": relative,
                        "content": chunk_text,
                        "startLine": start_line,
                        "endLine": end_line,
                        "tokens": tokenize(f"{relative}\n{chunk_text}"),
                    }
                )
    return chunks, indexed_files


def ensure_index(root: Path, chunk_chars: int, reindex: bool) -> tuple[Path, list[dict[str, object]], int]:
    index_path = root / INDEX_PATH
    if not reindex and index_path.exists():
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            chunks = payload.get("chunks", []) if isinstance(payload, dict) else []
            indexed_files = int(payload.get("indexedFiles", 0)) if isinstance(payload, dict) else 0
            if isinstance(chunks, list) and chunks:
                return index_path, chunks, indexed_files
        except (OSError, ValueError, TypeError):
            pass

    chunks, indexed_files = build_index(root, chunk_chars)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "root": root.as_posix(),
                "indexedFiles": indexed_files,
                "chunkCount": len(chunks),
                "chunkChars": chunk_chars,
                "chunks": chunks,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return index_path, chunks, indexed_files


def score_chunk(chunk: dict[str, object], query_tokens: list[str]) -> int:
    content_tokens = set(chunk.get("tokens", []))
    path = str(chunk.get("path", "")).lower()
    score = 0
    for token in query_tokens:
        if token in path:
            score += 6
        if token in content_tokens:
            score += 3
    if path.endswith(("architecture.md", "readme.md")):
        score += 4
    return score


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    query_tokens = tokenize(args.query)
    index_path, chunks, indexed_files = ensure_index(root, args.chunk_chars, args.reindex)
    scored = []
    for chunk in chunks:
        score = score_chunk(chunk, query_tokens)
        if score <= 0:
            continue
        scored.append(
            {
                "path": str(chunk.get("path", "")),
                "score": score,
                "content": str(chunk.get("content", ""))[: args.chunk_chars],
                "startLine": int(chunk.get("startLine", 1)),
                "endLine": int(chunk.get("endLine", 1)),
            }
        )

    scored.sort(key=lambda item: (-int(item["score"]), str(item["path"]), int(item["startLine"])))
    selected_chunks = scored[: max(1, args.max_chunks)]
    file_budget: list[str] = []
    selected_files = []
    for chunk in selected_chunks:
        if chunk["path"] not in file_budget:
            if len(file_budget) >= max(1, args.max_files):
                continue
            file_budget.append(chunk["path"])
        selected_files.append(chunk)

    print(f"RAG v1 indexed {indexed_files} file(s) and returned {len(selected_files)} chunk(s).")
    for item in selected_files:
        print(f"- {item['path']} lines {item['startLine']}-{item['endLine']} (score {item['score']})")

    if args.json_report:
        print(
            json.dumps(
                {
                    "root": root.as_posix(),
                    "query": args.query,
                    "indexPath": index_path.as_posix(),
                    "indexedFiles": indexed_files,
                    "chunkCount": len(chunks),
                    "files": selected_files,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
