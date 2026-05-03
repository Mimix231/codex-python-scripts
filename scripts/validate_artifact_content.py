#!/usr/bin/env python3
"""
validate_artifact_content.py

Validate one generated file candidate before Abyss writes it into the workspace.
This catches common failure modes such as Markdown or prose being emitted into
source or config files.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Sequence

try:
    import tomllib  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    tomllib = None


MARKDOWN_PATTERNS = [
    re.compile(r"^\s{0,3}#\s+", re.MULTILINE),
    re.compile(r"^\s{0,3}##\s+", re.MULTILINE),
    re.compile(r"^\s*[-*]\s+", re.MULTILINE),
    re.compile(r"^\s*\d+\.\s+", re.MULTILINE),
]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate generated file content before writing it into the workspace."
    )
    parser.add_argument("--path", required=True, help="Relative target file path.")
    parser.add_argument(
        "--project-type",
        default="general",
        help="Optional inferred project type for richer messages.",
    )
    parser.add_argument(
        "--json-report",
        action="store_true",
        help="Emit a JSON report to stdout.",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read candidate content from stdin.",
    )
    return parser.parse_args(argv)


def detect_role(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix in {".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".c", ".cpp", ".h", ".hpp", ".go", ".java", ".cs"}:
        return "source", "code"
    if suffix in {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf"}:
        return "config", "config"
    if suffix in {".md", ".rst", ".txt"}:
        return "doc", "document"
    if suffix in {".html", ".css", ".scss"}:
        return "source", "markup"
    return "unknown", "text"


def detect_content_type(content: str) -> str:
    stripped = content.strip()
    if not stripped:
        return "empty"
    if any(pattern.search(content) for pattern in MARKDOWN_PATTERNS):
        return "markdown"
    if stripped.startswith("{") or stripped.startswith("["):
        return "data"
    if re.search(r"^\s*(def |class |import |from |fn |pub |const |let |var |function |export )", content, re.MULTILINE):
        return "code"
    if re.search(r"^\s*\[[^\]]+\]\s*$", content, re.MULTILINE) or re.search(r"^\s*[A-Za-z0-9_.-]+\s*=\s*.+$", content, re.MULTILINE):
        return "config"
    return "prose"


def validate_json(content: str) -> list[str]:
    try:
        json.loads(content)
        return []
    except json.JSONDecodeError as error:
        return [f"Invalid JSON: {error.msg} at line {error.lineno}, column {error.colno}."]


def validate_toml(content: str) -> list[str]:
    if tomllib is None:
        return []
    try:
        tomllib.loads(content)
        return []
    except Exception as error:  # noqa: BLE001
        return [f"Invalid TOML: {error}"]


def validate_python(content: str) -> list[str]:
    try:
        ast.parse(content)
        return []
    except SyntaxError as error:
        line = error.lineno or 1
        column = error.offset or 1
        return [f"Invalid Python syntax: {error.msg} at line {line}, column {column}."]


def validate_generic_code(content: str, suffix: str) -> list[str]:
    stripped = content.strip()
    errors: list[str] = []
    if not stripped:
        errors.append("Generated file is empty.")
        return errors

    if detect_content_type(content) == "markdown":
        errors.append("Generated content looks like Markdown, not code.")

    code_signals = [
        "{",
        "}",
        "(",
        ")",
        ";",
        "=>",
        "function",
        "class ",
        "import ",
        "export ",
        "const ",
        "let ",
        "var ",
        "fn ",
        "impl ",
        "pub ",
    ]
    if suffix in {".ts", ".tsx", ".js", ".jsx", ".rs", ".c", ".cpp", ".h", ".hpp"} and not any(
        signal in content for signal in code_signals
    ):
        errors.append("Generated content does not look like executable source code.")
    return errors


def validate_markup(content: str) -> list[str]:
    if not content.strip():
        return ["Generated file is empty."]
    if detect_content_type(content) == "markdown":
        return ["Generated content looks like Markdown, not markup or styling."]
    return []


def build_report(path_str: str, project_type: str, content: str) -> dict[str, object]:
    path = Path(path_str)
    role, expected_content = detect_role(path)
    detected_content = detect_content_type(content)
    suffix = path.suffix.lower()
    parser_name = "heuristic"
    errors: list[str] = []
    warnings: list[str] = []

    if role == "doc":
        parser_name = "text"
    elif suffix == ".json":
        parser_name = "json"
        errors.extend(validate_json(content))
    elif suffix == ".toml":
        parser_name = "toml"
        errors.extend(validate_toml(content))
    elif suffix == ".py":
        parser_name = "python"
        errors.extend(validate_python(content))
    elif suffix in {".html", ".css", ".scss"}:
        parser_name = "markup"
        errors.extend(validate_markup(content))
    elif role == "source":
        parser_name = "heuristic-code"
        errors.extend(validate_generic_code(content, suffix))

    if role in {"source", "config"} and detected_content in {"markdown", "prose"}:
        errors.append(
            f"Detected {detected_content} content in a {role} artifact. This file should contain {expected_content}."
        )

    if role == "config" and suffix == ".toml":
        lowered = content.lower()
        if "## " in content or "# " in content and "[project]" not in lowered and "[tool." not in lowered:
            warnings.append("TOML file contains heading-like markers that often indicate prose leakage.")

    summary = (
        f"{path.as_posix()} accepted as a {role} artifact."
        if not errors
        else f"{path.as_posix()} rejected because the generated content does not match its file role."
    )

    return {
        "path": path.as_posix(),
        "projectType": project_type,
        "role": role,
        "expectedContent": expected_content,
        "detectedContentType": detected_content,
        "parser": parser_name,
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "summary": summary,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    content = sys.stdin.read() if args.stdin else ""
    report = build_report(args.path, args.project_type, content)

    if args.json_report:
        print(json.dumps(report, indent=2))
    else:
        print(report["summary"])
        for error in report["errors"]:
            print(f"ERROR: {error}")
        for warning in report["warnings"]:
            print(f"WARNING: {warning}")

    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
