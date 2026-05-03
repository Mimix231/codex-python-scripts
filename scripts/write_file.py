#!/usr/bin/env python3
"""
write_file.py

Deterministic bulk file writer for creating or updating one or many files in a
single pass. This is intended as a safer companion to patch-based editing:

- preview is the default mode
- content can come from literals, files, stdin, built-in templates, or JSON ops
- writes can replace, create, append, prepend, or insert around anchors
- variables can be expanded into templates and literal content
- multiple operations can be applied to the same file in sequence
- optional backups, chmod, and JSON reports are supported

Examples:

    python ai_tools/write_file.py ^
      --path notes.txt ^
      --content "hello world" ^
      --write-mode create ^
      --mode preview

    Get-Content draft.txt -Raw | python ai_tools/write_file.py ^
      --path src/example.py ^
      --stdin ^
      --write-mode replace ^
      --ensure-parent ^
      --mode apply

    python ai_tools/write_file.py ^
      --files src/a.c src/b.c ^
      --template c-source ^
      --var BODY="// TODO" ^
      --write-mode create ^
      --mode preview

    python ai_tools/write_file.py ^
      --path README.md ^
      --content "## New section`n`nDetails." ^
      --write-mode insert-after ^
      --anchor "# Existing heading" ^
      --mode apply

    python ai_tools/write_file.py ^
      --ops-file ai_tools/write_ops.json ^
      --mode preview ^
      --json-report
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import stat
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


DEFAULT_MODE = "preview"
DEFAULT_WRITE_MODE = "replace"
DEFAULT_ENCODING = "utf-8"
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass(slots=True)
class TemplateDef:
    name: str
    description: str
    content: str


@dataclass(slots=True)
class OperationSpec:
    source: str
    path: Path
    write_mode: str
    content: str
    content_origin: str
    anchor: str | None
    regex_anchor: str | None
    all_matches: bool
    ignore_missing_anchor: bool
    force: bool
    ensure_parent: bool
    encoding: str
    newline: str
    final_newline: str
    dedent: bool
    strip_trailing_whitespace: bool
    trim_leading_blank_lines: bool
    trim_trailing_blank_lines: bool
    chmod: str | None


@dataclass(slots=True)
class OperationResult:
    source: str
    write_mode: str
    content_origin: str
    changed: bool
    created_file: bool
    bytes_delta: int
    anchors_matched: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FileOutcome:
    path: Path
    existed_before: bool
    original_text: str
    final_text: str
    created: bool
    changed: bool
    operations: list[OperationResult]
    encoding: str
    chmod: str | None
    ensure_parent: bool


def build_templates() -> dict[str, TemplateDef]:
    return {
        "empty": TemplateDef("empty", "Create an empty file.", ""),
        "text": TemplateDef("text", "Plain text file with a body placeholder.", "{{BODY}}"),
        "markdown": TemplateDef(
            "markdown",
            "Basic Markdown document.",
            "# {{TITLE}}\n\n{{SUMMARY}}\n",
        ),
        "python-script": TemplateDef(
            "python-script",
            "Executable Python script.",
            """#!/usr/bin/env python3
\"\"\"
{{TITLE}}
\"\"\"

from __future__ import annotations


def main() -> int:
    {{BODY}}
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
        ),
        "shell-script": TemplateDef(
            "shell-script",
            "POSIX shell script.",
            """#!/usr/bin/env bash
set -euo pipefail

{{BODY}}
""",
        ),
        "powershell-script": TemplateDef(
            "powershell-script",
            "PowerShell script.",
            """param()
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

{{BODY}}
""",
        ),
        "lua-script": TemplateDef(
            "lua-script",
            "Lua script.",
            """local M = {}

function M.main()
    {{BODY}}
end

return M
""",
        ),
        "json-object": TemplateDef(
            "json-object",
            "Simple JSON object.",
            '{\n  "name": "{{STEM}}",\n  "version": "0.1.0"\n}\n',
        ),
        "yaml": TemplateDef(
            "yaml",
            "Basic YAML document.",
            "name: {{STEM}}\ndescription: {{SUMMARY}}\n",
        ),
        "toml": TemplateDef(
            "toml",
            "Basic TOML document.",
            'name = "{{STEM}}"\nsummary = "{{SUMMARY}}"\n',
        ),
        "ini": TemplateDef(
            "ini",
            "Basic INI document.",
            "[{{SECTION}}]\nkey = value\n",
        ),
        "html": TemplateDef(
            "html",
            "Minimal HTML page.",
            """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{TITLE}}</title>
</head>
<body>
  {{BODY}}
</body>
</html>
""",
        ),
        "c-header": TemplateDef(
            "c-header",
            "C header with include guard.",
            """#ifndef {{INCLUDE_GUARD}}
#define {{INCLUDE_GUARD}}

#ifdef __cplusplus
extern "C" {
#endif

{{BODY}}

#ifdef __cplusplus
}
#endif

#endif /* {{INCLUDE_GUARD}} */
""",
        ),
        "c-source": TemplateDef(
            "c-source",
            "C source file paired with a header of the same stem.",
            """#include "{{STEM}}.h"

{{BODY}}
""",
        ),
        "cpp-header": TemplateDef(
            "cpp-header",
            "C++ header with include guard and namespace placeholder.",
            """#ifndef {{INCLUDE_GUARD}}
#define {{INCLUDE_GUARD}}

namespace {{NAMESPACE}} {

{{BODY}}

}  // namespace {{NAMESPACE}}

#endif /* {{INCLUDE_GUARD}} */
""",
        ),
        "cpp-source": TemplateDef(
            "cpp-source",
            "C++ source file paired with a header of the same stem.",
            """#include "{{STEM}}.h"

namespace {{NAMESPACE}} {

{{BODY}}

}  // namespace {{NAMESPACE}}
""",
        ),
        "sql": TemplateDef(
            "sql",
            "SQL migration or query file.",
            "-- {{TITLE}}\n\n{{BODY}}\n",
        ),
    }


TEMPLATES = build_templates()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write files in bulk with preview-first safety."
    )

    target_group = parser.add_argument_group("targets")
    target_group.add_argument(
        "--path",
        action="append",
        default=[],
        help="Single target path. May be repeated.",
    )
    target_group.add_argument(
        "--files",
        nargs="+",
        default=[],
        help="Explicit target paths.",
    )
    target_group.add_argument(
        "--bulk",
        action="append",
        default=[],
        help=(
            "Bulk file list. Accepts JSON-like arrays such as "
            "'[a.txt,b.txt]' or comma-separated paths."
        ),
    )
    target_group.add_argument(
        "--glob",
        action="append",
        default=[],
        help="Glob pattern to expand from the current working directory.",
    )
    target_group.add_argument(
        "--file-list",
        action="append",
        default=[],
        help="Text file containing one target path per line.",
    )
    target_group.add_argument(
        "--ops-file",
        action="append",
        default=[],
        help=(
            "JSON file describing one or many operations. Accepts either a list "
            "of objects or an object with an 'operations' array."
        ),
    )

    content_group = parser.add_argument_group("content sources")
    content_group.add_argument(
        "--content",
        help="Literal content to write.",
    )
    content_group.add_argument(
        "--content-file",
        help="Read content from a file.",
    )
    content_group.add_argument(
        "--stdin",
        action="store_true",
        help="Read content from standard input once and reuse it for all CLI targets.",
    )
    content_group.add_argument(
        "--template",
        help="Use a built-in template.",
    )
    content_group.add_argument(
        "--template-file",
        help="Read template text from a file before variable expansion.",
    )
    content_group.add_argument(
        "--var",
        action="append",
        default=[],
        help="Template/content variable in KEY=VALUE form. May be repeated.",
    )
    content_group.add_argument(
        "--var-file",
        action="append",
        default=[],
        help="Template/content variable in KEY=PATH form. File contents become the value.",
    )
    content_group.add_argument(
        "--vars-json",
        action="append",
        default=[],
        help="JSON file containing an object of template/content variables.",
    )
    content_group.add_argument(
        "--strict-vars",
        action="store_true",
        help="Fail if a {{VAR}} placeholder has no resolved value.",
    )
    content_group.add_argument(
        "--list-templates",
        action="store_true",
        help="List built-in templates and exit.",
    )

    write_group = parser.add_argument_group("write behavior")
    write_group.add_argument(
        "--write-mode",
        choices=[
            "create",
            "replace",
            "append",
            "prepend",
            "insert-before",
            "insert-after",
        ],
        default=DEFAULT_WRITE_MODE,
        help="How to write the content into the target file.",
    )
    write_group.add_argument(
        "--anchor",
        help="Literal anchor for insert-before or insert-after.",
    )
    write_group.add_argument(
        "--regex-anchor",
        help="Regex anchor for insert-before or insert-after.",
    )
    write_group.add_argument(
        "--all-matches",
        action="store_true",
        help="For insert modes, apply the insertion to all anchor matches.",
    )
    write_group.add_argument(
        "--ignore-missing-anchor",
        action="store_true",
        help="For insert modes, treat missing anchors as a no-op instead of an error.",
    )
    write_group.add_argument(
        "--force",
        action="store_true",
        help="Allow create to overwrite, or allow other modes to create missing files.",
    )
    write_group.add_argument(
        "--ensure-parent",
        action="store_true",
        help="Create parent directories before writing in apply mode.",
    )
    write_group.add_argument(
        "--backup",
        action="store_true",
        help="Create .bak files before overwriting existing files in apply mode.",
    )
    write_group.add_argument(
        "--backup-suffix",
        default=".bak",
        help="Suffix for backup files. Default: .bak",
    )
    write_group.add_argument(
        "--chmod",
        help="Optional chmod mode to apply after writing, for example 755 or +x.",
    )

    format_group = parser.add_argument_group("formatting")
    format_group.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"Text encoding for reads and writes. Default: {DEFAULT_ENCODING}",
    )
    format_group.add_argument(
        "--newline",
        choices=["keep", "lf", "crlf"],
        default="keep",
        help="Final newline style for the whole file.",
    )
    format_group.add_argument(
        "--final-newline",
        choices=["always", "never", "keep"],
        default="keep",
        help="Whether the final file should end with a trailing newline.",
    )
    format_group.add_argument(
        "--dedent",
        action="store_true",
        help="Dedent the input content before writing it.",
    )
    format_group.add_argument(
        "--strip-trailing-whitespace",
        action="store_true",
        help="Strip trailing spaces/tabs from the input content before writing it.",
    )
    format_group.add_argument(
        "--trim-leading-blank-lines",
        action="store_true",
        help="Trim leading blank lines from the input content.",
    )
    format_group.add_argument(
        "--trim-trailing-blank-lines",
        action="store_true",
        help="Trim trailing blank lines from the input content.",
    )

    control_group = parser.add_argument_group("execution")
    control_group.add_argument(
        "--mode",
        choices=["preview", "apply"],
        default=DEFAULT_MODE,
        help="Preview diffs or apply changes.",
    )
    control_group.add_argument(
        "--context",
        type=int,
        default=3,
        help="Unified diff context lines in preview mode.",
    )
    control_group.add_argument(
        "--fail-on-noop",
        action="store_true",
        help="Exit with an error if no file content would change.",
    )
    control_group.add_argument(
        "--expect-total",
        type=int,
        help="Expected number of changed operations.",
    )
    control_group.add_argument(
        "--expect-files",
        type=int,
        help="Expected number of changed files.",
    )
    control_group.add_argument(
        "--expect-per-file",
        type=int,
        help="Expected number of changed operations per changed file.",
    )
    control_group.add_argument(
        "--json-report",
        action="store_true",
        help="Emit a JSON summary after preview/apply output.",
    )

    return parser.parse_args(argv)


def parse_bulk_value(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            items = [item.strip() for item in inner.split(",")]
            return [strip_wrapping_quotes(item) for item in items if item.strip()]
        if not isinstance(parsed, list):
            raise SystemExit(f"bulk value must decode to a list: {raw!r}")
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in text.split(",") if item.strip()]


def strip_wrapping_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def read_text(path: Path, encoding: str) -> str:
    try:
        return path.read_text(encoding=encoding)
    except FileNotFoundError:
        raise SystemExit(f"file not found: {path}")


def write_text(path: Path, text: str, encoding: str) -> None:
    with path.open("w", encoding=encoding, newline="") as handle:
        handle.write(text)


def make_include_guard(path: Path) -> str:
    raw = str(path).replace("\\", "/")
    stem = raw.upper()
    stem = re.sub(r"[^A-Z0-9]+", "_", stem).strip("_")
    if not stem:
        stem = "GENERATED_HEADER"
    return f"{stem}_"


def default_variables_for_path(path: Path) -> dict[str, str]:
    now = datetime.now()
    stem = path.stem or path.name or "file"
    parent_name = path.parent.name or "."
    namespace = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_") or "generated"
    return {
        "PATH": str(path),
        "ABS_PATH": str(path.resolve()),
        "BASENAME": path.name,
        "STEM": stem,
        "SUFFIX": path.suffix,
        "PARENT": str(path.parent),
        "PARENT_NAME": parent_name,
        "TITLE": stem.replace("_", " ").replace("-", " ").strip() or path.name,
        "SUMMARY": "",
        "SECTION": "default",
        "BODY": "",
        "NAMESPACE": namespace,
        "INCLUDE_GUARD": make_include_guard(path),
        "DATE": now.strftime("%Y-%m-%d"),
        "YEAR": now.strftime("%Y"),
        "MONTH": now.strftime("%m"),
        "DAY": now.strftime("%d"),
    }


def parse_key_value(raw: str, flag_name: str) -> tuple[str, str]:
    if "=" not in raw:
        raise SystemExit(f"{flag_name} expects KEY=VALUE: {raw!r}")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise SystemExit(f"{flag_name} expects a non-empty key: {raw!r}")
    return key, value


def load_json_object(path: Path, encoding: str) -> dict[str, Any]:
    try:
        payload = json.loads(read_text(path, encoding))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"JSON variables file must contain an object: {path}")
    return payload


def resolve_variables(
    path: Path,
    encoding: str,
    inline_vars: Iterable[str],
    file_vars: Iterable[str],
    json_var_files: Iterable[str],
    extra_vars: dict[str, Any] | None = None,
) -> dict[str, str]:
    variables = default_variables_for_path(path)
    for json_file in json_var_files:
        payload = load_json_object(Path(json_file), encoding)
        for key, value in payload.items():
            variables[str(key)] = "" if value is None else str(value)

    for raw in inline_vars:
        key, value = parse_key_value(raw, "--var")
        variables[key] = value

    for raw in file_vars:
        key, file_path_raw = parse_key_value(raw, "--var-file")
        file_path = Path(file_path_raw)
        variables[key] = read_text(file_path, encoding)

    if extra_vars:
        for key, value in extra_vars.items():
            variables[str(key)] = "" if value is None else str(value)

    return variables


def render_placeholders(text: str, variables: dict[str, str], strict: bool) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in variables:
            return variables[key]
        if strict:
            raise SystemExit(f"missing template/content variable: {key}")
        return match.group(0)

    return PLACEHOLDER_RE.sub(replace, text)


def list_templates() -> None:
    print("Built-in templates:")
    for name in sorted(TEMPLATES):
        print(f"- {name}: {TEMPLATES[name].description}")


def normalize_input_content(text: str, spec: OperationSpec) -> str:
    updated = text

    if spec.dedent:
        updated = textwrap.dedent(updated)

    if spec.trim_leading_blank_lines:
        updated = re.sub(r"\A(?:[ \t]*\n)+", "", updated)

    if spec.trim_trailing_blank_lines:
        updated = re.sub(r"(?:\n[ \t]*)+\Z", "", updated)

    if spec.strip_trailing_whitespace:
        lines = updated.splitlines(True)
        cleaned: list[str] = []
        for line in lines:
            newline_part = ""
            content_part = line
            if line.endswith("\r\n"):
                newline_part = "\r\n"
                content_part = line[:-2]
            elif line.endswith("\n"):
                newline_part = "\n"
                content_part = line[:-1]
            cleaned.append(content_part.rstrip(" \t") + newline_part)
        updated = "".join(cleaned)

    return updated


def detect_original_newline(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def convert_newlines(text: str, newline_mode: str, original_text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline_mode == "keep":
        target = detect_original_newline(original_text) if original_text else os.linesep
    elif newline_mode == "crlf":
        target = "\r\n"
    else:
        target = "\n"
    return normalized.replace("\n", target)


def apply_final_newline_policy(text: str, policy: str, original_text: str) -> str:
    if not text:
        return text

    if policy == "always":
        if text.endswith("\n") or text.endswith("\r"):
            return text
        if "\r\n" in text:
            return text + "\r\n"
        return text + "\n"

    if policy == "never":
        return text.rstrip("\r\n")

    if original_text:
        had_final_newline = original_text.endswith("\n") or original_text.endswith("\r")
        if had_final_newline:
            if text.endswith("\n") or text.endswith("\r"):
                return text
            if "\r\n" in original_text:
                return text + "\r\n"
            return text + "\n"
        return text.rstrip("\r\n")

    return text


def collect_targets_from_sources(
    *,
    direct_paths: Iterable[str] = (),
    files: Iterable[str] = (),
    bulk_values: Iterable[str] = (),
    glob_values: Iterable[str] = (),
    file_lists: Iterable[str] = (),
) -> list[Path]:
    ordered: list[Path] = []
    seen: set[Path] = set()

    def add_path(path: Path) -> None:
        if path in seen:
            return
        seen.add(path)
        ordered.append(path)

    for raw in direct_paths:
        stripped = str(raw).strip()
        if stripped:
            add_path(Path(stripped))

    for raw in files:
        stripped = str(raw).strip()
        if stripped:
            add_path(Path(stripped))

    for bulk in bulk_values:
        for item in parse_bulk_value(bulk):
            add_path(Path(item))

    for file_list in file_lists:
        list_path = Path(file_list)
        for line in read_text(list_path, DEFAULT_ENCODING).splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                add_path(Path(stripped))

    for pattern in glob_values:
        matches = sorted(Path.cwd().glob(pattern))
        if not matches:
            raise SystemExit(f"glob matched no paths: {pattern!r}")
        for match in matches:
            if match.is_dir():
                raise SystemExit(f"glob matched a directory, not a file path: {match}")
            add_path(match)

    return ordered


def select_content_source_name(source_name: str | None) -> str:
    return source_name or "empty"


def load_template_content(name: str, path: Path) -> str:
    if name not in TEMPLATES:
        raise SystemExit(f"unknown template: {name!r}")
    template = TEMPLATES[name]
    return template.content


def load_content_text(
    *,
    path: Path,
    encoding: str,
    content: str | None,
    content_file: str | None,
    stdin_text: str | None,
    use_stdin: bool,
    template: str | None,
    template_file: str | None,
    variables: dict[str, str],
    strict_vars: bool,
) -> tuple[str, str]:
    provided = sum(
        1
        for value in [
            content is not None,
            content_file is not None,
            use_stdin,
            template is not None,
            template_file is not None,
        ]
        if value
    )

    if provided > 1:
        raise SystemExit(
            f"multiple content sources provided for {path}; choose only one of "
            "--content, --content-file, --stdin, --template, or --template-file"
        )

    if content is not None:
        raw = content
        origin = "literal"
    elif content_file is not None:
        raw = read_text(Path(content_file), encoding)
        origin = f"file:{content_file}"
    elif use_stdin:
        raw = stdin_text or ""
        origin = "stdin"
    elif template is not None:
        raw = load_template_content(template, path)
        origin = f"template:{template}"
    elif template_file is not None:
        raw = read_text(Path(template_file), encoding)
        origin = f"template-file:{template_file}"
    else:
        raw = ""
        origin = "empty"

    rendered = render_placeholders(raw, variables, strict_vars)
    return rendered, origin


def find_literal_matches(text: str, needle: str) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    if not needle:
        raise SystemExit("anchor must not be empty")
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            break
        matches.append((index, index + len(needle)))
        start = index + len(needle)
    return matches


def find_regex_matches(text: str, pattern: str) -> list[tuple[int, int]]:
    try:
        regex = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise SystemExit(f"invalid regex anchor {pattern!r}: {exc}") from exc
    return [(match.start(), match.end()) for match in regex.finditer(text)]


def apply_insert_mode(
    text: str,
    content: str,
    *,
    before: bool,
    anchor: str | None,
    regex_anchor: str | None,
    all_matches: bool,
    ignore_missing_anchor: bool,
) -> tuple[str, int]:
    if bool(anchor) == bool(regex_anchor):
        raise SystemExit(
            "insert-before/insert-after requires exactly one of --anchor or --regex-anchor"
        )

    matches = (
        find_literal_matches(text, anchor) if anchor is not None else find_regex_matches(text, regex_anchor or "")
    )

    if not matches:
        if ignore_missing_anchor:
            return text, 0
        target = anchor if anchor is not None else regex_anchor
        raise SystemExit(f"anchor not found: {target!r}")

    if not all_matches:
        matches = matches[:1]

    pieces = text
    offset = 0
    for start, end in matches:
        position = start if before else end
        insert_at = position + offset
        pieces = pieces[:insert_at] + content + pieces[insert_at:]
        offset += len(content)
    return pieces, len(matches)


def apply_operation_to_text(
    *,
    spec: OperationSpec,
    current_text: str,
    existed_before: bool,
) -> tuple[str, OperationResult]:
    if spec.write_mode == "create":
        if existed_before and not spec.force:
            raise SystemExit(
                f"target already exists and --force was not provided for create: {spec.path}"
            )
        updated = spec.content
        created_file = not existed_before
        anchors_matched = 0
    elif spec.write_mode == "replace":
        if not existed_before and not spec.force:
            raise SystemExit(
                f"target does not exist and --force was not provided for replace: {spec.path}"
            )
        updated = spec.content
        created_file = not existed_before
        anchors_matched = 0
    elif spec.write_mode == "append":
        if not existed_before and not spec.force:
            raise SystemExit(
                f"target does not exist and --force was not provided for append: {spec.path}"
            )
        updated = current_text + spec.content
        created_file = not existed_before
        anchors_matched = 0
    elif spec.write_mode == "prepend":
        if not existed_before and not spec.force:
            raise SystemExit(
                f"target does not exist and --force was not provided for prepend: {spec.path}"
            )
        updated = spec.content + current_text
        created_file = not existed_before
        anchors_matched = 0
    elif spec.write_mode == "insert-before":
        if not existed_before and not spec.force:
            raise SystemExit(
                f"target does not exist and --force was not provided for insert-before: {spec.path}"
            )
        updated, anchors_matched = apply_insert_mode(
            current_text,
            spec.content,
            before=True,
            anchor=spec.anchor,
            regex_anchor=spec.regex_anchor,
            all_matches=spec.all_matches,
            ignore_missing_anchor=spec.ignore_missing_anchor,
        )
        created_file = not existed_before
    elif spec.write_mode == "insert-after":
        if not existed_before and not spec.force:
            raise SystemExit(
                f"target does not exist and --force was not provided for insert-after: {spec.path}"
            )
        updated, anchors_matched = apply_insert_mode(
            current_text,
            spec.content,
            before=False,
            anchor=spec.anchor,
            regex_anchor=spec.regex_anchor,
            all_matches=spec.all_matches,
            ignore_missing_anchor=spec.ignore_missing_anchor,
        )
        created_file = not existed_before
    else:
        raise SystemExit(f"unsupported write mode: {spec.write_mode}")

    updated = convert_newlines(updated, spec.newline, current_text)
    updated = apply_final_newline_policy(updated, spec.final_newline, current_text)
    changed = (not existed_before) or (updated != current_text)

    return updated, OperationResult(
        source=spec.source,
        write_mode=spec.write_mode,
        content_origin=spec.content_origin,
        changed=changed,
        created_file=created_file,
        bytes_delta=len(updated) - len(current_text),
        anchors_matched=anchors_matched,
        details={"path": str(spec.path)},
    )


def load_ops_payload(path: Path, encoding: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(read_text(path, encoding))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in ops file {path}: {exc}") from exc

    if isinstance(payload, list):
        operations = payload
    elif isinstance(payload, dict) and "operations" in payload:
        operations = payload["operations"]
    else:
        raise SystemExit(
            f"ops file must be a list or an object with an 'operations' key: {path}"
        )

    if not isinstance(operations, list):
        raise SystemExit(f"'operations' must be a list in ops file: {path}")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(operations, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"ops file item #{index} in {path} is not an object")
        normalized.append(item)
    return normalized


def operation_defaults_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "write_mode": args.write_mode,
        "anchor": args.anchor,
        "regex_anchor": args.regex_anchor,
        "all_matches": args.all_matches,
        "ignore_missing_anchor": args.ignore_missing_anchor,
        "force": args.force,
        "ensure_parent": args.ensure_parent,
        "encoding": args.encoding,
        "newline": args.newline,
        "final_newline": args.final_newline,
        "dedent": args.dedent,
        "strip_trailing_whitespace": args.strip_trailing_whitespace,
        "trim_leading_blank_lines": args.trim_leading_blank_lines,
        "trim_trailing_blank_lines": args.trim_trailing_blank_lines,
        "chmod": args.chmod,
        "strict_vars": args.strict_vars,
    }


def build_cli_operation_specs(args: argparse.Namespace, stdin_text: str | None) -> list[OperationSpec]:
    target_paths = collect_targets_from_sources(
        direct_paths=args.path,
        files=args.files,
        bulk_values=args.bulk,
        glob_values=args.glob,
        file_lists=args.file_list,
    )
    if not target_paths:
        return []

    specs: list[OperationSpec] = []
    defaults = operation_defaults_from_args(args)

    for target in target_paths:
        variables = resolve_variables(
            target,
            args.encoding,
            args.var,
            args.var_file,
            args.vars_json,
        )
        content_text, content_origin = load_content_text(
            path=target,
            encoding=args.encoding,
            content=args.content,
            content_file=args.content_file,
            stdin_text=stdin_text,
            use_stdin=args.stdin,
            template=args.template,
            template_file=args.template_file,
            variables=variables,
            strict_vars=args.strict_vars,
        )
        temp_spec = OperationSpec(
            source="cli",
            path=target,
            write_mode=defaults["write_mode"],
            content=normalize_input_content(
                content_text,
                OperationSpec(
                    source="cli",
                    path=target,
                    write_mode=defaults["write_mode"],
                    content="",
                    content_origin=content_origin,
                    anchor=defaults["anchor"],
                    regex_anchor=defaults["regex_anchor"],
                    all_matches=defaults["all_matches"],
                    ignore_missing_anchor=defaults["ignore_missing_anchor"],
                    force=defaults["force"],
                    ensure_parent=defaults["ensure_parent"],
                    encoding=defaults["encoding"],
                    newline=defaults["newline"],
                    final_newline=defaults["final_newline"],
                    dedent=defaults["dedent"],
                    strip_trailing_whitespace=defaults["strip_trailing_whitespace"],
                    trim_leading_blank_lines=defaults["trim_leading_blank_lines"],
                    trim_trailing_blank_lines=defaults["trim_trailing_blank_lines"],
                    chmod=defaults["chmod"],
                ),
            ),
            content_origin=content_origin,
            anchor=defaults["anchor"],
            regex_anchor=defaults["regex_anchor"],
            all_matches=defaults["all_matches"],
            ignore_missing_anchor=defaults["ignore_missing_anchor"],
            force=defaults["force"],
            ensure_parent=defaults["ensure_parent"],
            encoding=defaults["encoding"],
            newline=defaults["newline"],
            final_newline=defaults["final_newline"],
            dedent=defaults["dedent"],
            strip_trailing_whitespace=defaults["strip_trailing_whitespace"],
            trim_leading_blank_lines=defaults["trim_leading_blank_lines"],
            trim_trailing_blank_lines=defaults["trim_trailing_blank_lines"],
            chmod=defaults["chmod"],
        )
        specs.append(temp_spec)

    return specs


def extract_targets_from_op(item: dict[str, Any], source_name: str) -> list[Path]:
    direct_paths: list[str] = []
    files: list[str] = []
    bulk_values: list[str] = []
    glob_values: list[str] = []
    file_lists: list[str] = []

    if "path" in item:
        direct_paths.append(str(item["path"]))
    if "paths" in item:
        if not isinstance(item["paths"], list):
            raise SystemExit(f"{source_name}: 'paths' must be a list")
        direct_paths.extend(str(value) for value in item["paths"])
    if "files" in item:
        if not isinstance(item["files"], list):
            raise SystemExit(f"{source_name}: 'files' must be a list")
        files.extend(str(value) for value in item["files"])
    if "bulk" in item:
        bulk_raw = item["bulk"]
        if isinstance(bulk_raw, list):
            bulk_values.append(json.dumps(bulk_raw))
        else:
            bulk_values.append(str(bulk_raw))
    if "glob" in item:
        glob_raw = item["glob"]
        if isinstance(glob_raw, list):
            glob_values.extend(str(value) for value in glob_raw)
        else:
            glob_values.append(str(glob_raw))
    if "file_list" in item:
        file_list_raw = item["file_list"]
        if isinstance(file_list_raw, list):
            file_lists.extend(str(value) for value in file_list_raw)
        else:
            file_lists.append(str(file_list_raw))

    paths = collect_targets_from_sources(
        direct_paths=direct_paths,
        files=files,
        bulk_values=bulk_values,
        glob_values=glob_values,
        file_lists=file_lists,
    )
    if not paths:
        raise SystemExit(f"{source_name}: operation has no target paths")
    return paths


def value_or_default(item: dict[str, Any], key: str, defaults: dict[str, Any]) -> Any:
    return item[key] if key in item else defaults[key]


def load_vars_from_mapping(item: dict[str, Any]) -> tuple[list[str], list[str], list[str], dict[str, Any]]:
    inline_vars: list[str] = []
    file_vars: list[str] = []
    json_vars: list[str] = []
    extra_vars: dict[str, Any] = {}

    raw_vars = item.get("vars")
    if raw_vars is not None:
        if isinstance(raw_vars, dict):
            extra_vars.update(raw_vars)
        elif isinstance(raw_vars, list):
            for entry in raw_vars:
                inline_vars.append(str(entry))
        else:
            raise SystemExit("ops item 'vars' must be an object or a list")

    raw_var_files = item.get("var_files")
    if raw_var_files is not None:
        if isinstance(raw_var_files, dict):
            for key, value in raw_var_files.items():
                file_vars.append(f"{key}={value}")
        elif isinstance(raw_var_files, list):
            for entry in raw_var_files:
                file_vars.append(str(entry))
        else:
            raise SystemExit("ops item 'var_files' must be an object or a list")

    raw_vars_json = item.get("vars_json")
    if raw_vars_json is not None:
        if isinstance(raw_vars_json, list):
            json_vars.extend(str(value) for value in raw_vars_json)
        else:
            json_vars.append(str(raw_vars_json))

    return inline_vars, file_vars, json_vars, extra_vars


def build_ops_file_specs(
    args: argparse.Namespace,
    stdin_text: str | None,
) -> list[OperationSpec]:
    del stdin_text
    specs: list[OperationSpec] = []
    defaults = operation_defaults_from_args(args)

    for ops_path_raw in args.ops_file:
        ops_path = Path(ops_path_raw)
        items = load_ops_payload(ops_path, args.encoding)
        for index, item in enumerate(items, start=1):
            source_name = f"{ops_path}#{index}"
            targets = extract_targets_from_op(item, source_name)
            inline_vars, file_vars, json_vars, extra_vars = load_vars_from_mapping(item)

            for target in targets:
                variables = resolve_variables(
                    target,
                    str(item.get("encoding", args.encoding)),
                    [*args.var, *inline_vars],
                    [*args.var_file, *file_vars],
                    [*args.vars_json, *json_vars],
                    extra_vars=extra_vars,
                )
                content_text, content_origin = load_content_text(
                    path=target,
                    encoding=str(item.get("encoding", args.encoding)),
                    content=item.get("content", args.content if "content" not in item else None),
                    content_file=item.get("content_file", None),
                    stdin_text=None,
                    use_stdin=False,
                    template=item.get("template", None),
                    template_file=item.get("template_file", None),
                    variables=variables,
                    strict_vars=bool(item.get("strict_vars", defaults["strict_vars"])),
                )

                spec = OperationSpec(
                    source=source_name,
                    path=target,
                    write_mode=str(value_or_default(item, "write_mode", defaults)),
                    content="",
                    content_origin=content_origin,
                    anchor=(
                        None
                        if item.get("anchor") is None
                        else str(item.get("anchor"))
                    )
                    if "anchor" in item or defaults["anchor"] is not None
                    else None,
                    regex_anchor=(
                        None
                        if item.get("regex_anchor") is None
                        else str(item.get("regex_anchor"))
                    )
                    if "regex_anchor" in item or defaults["regex_anchor"] is not None
                    else None,
                    all_matches=bool(value_or_default(item, "all_matches", defaults)),
                    ignore_missing_anchor=bool(
                        value_or_default(item, "ignore_missing_anchor", defaults)
                    ),
                    force=bool(value_or_default(item, "force", defaults)),
                    ensure_parent=bool(value_or_default(item, "ensure_parent", defaults)),
                    encoding=str(value_or_default(item, "encoding", defaults)),
                    newline=str(value_or_default(item, "newline", defaults)),
                    final_newline=str(value_or_default(item, "final_newline", defaults)),
                    dedent=bool(value_or_default(item, "dedent", defaults)),
                    strip_trailing_whitespace=bool(
                        value_or_default(item, "strip_trailing_whitespace", defaults)
                    ),
                    trim_leading_blank_lines=bool(
                        value_or_default(item, "trim_leading_blank_lines", defaults)
                    ),
                    trim_trailing_blank_lines=bool(
                        value_or_default(item, "trim_trailing_blank_lines", defaults)
                    ),
                    chmod=(
                        None
                        if value_or_default(item, "chmod", defaults) is None
                        else str(value_or_default(item, "chmod", defaults))
                    ),
                )
                spec.content = normalize_input_content(content_text, spec)
                specs.append(spec)

    return specs


def build_all_specs(args: argparse.Namespace, stdin_text: str | None) -> list[OperationSpec]:
    specs = build_cli_operation_specs(args, stdin_text)
    specs.extend(build_ops_file_specs(args, stdin_text))
    return specs


def load_file_state(path: Path, encoding: str) -> tuple[bool, str]:
    if path.exists():
        if path.is_dir():
            raise SystemExit(f"target is a directory, not a file: {path}")
        return True, read_text(path, encoding)
    return False, ""


def process_operations(specs: list[OperationSpec]) -> list[FileOutcome]:
    original_state: dict[Path, tuple[bool, str, str]] = {}
    current_state: dict[Path, str] = {}
    current_exists: dict[Path, bool] = {}
    changed_ops: dict[Path, list[OperationResult]] = {}
    final_encoding: dict[Path, str] = {}
    final_chmod: dict[Path, str | None] = {}
    ensure_parent: dict[Path, bool] = {}

    for spec in specs:
        if spec.path not in original_state:
            existed, text = load_file_state(spec.path, spec.encoding)
            original_state[spec.path] = (existed, text, spec.encoding)
            current_state[spec.path] = text
            current_exists[spec.path] = existed

        existed_before = current_exists[spec.path]
        current_text = current_state[spec.path]
        updated_text, op_result = apply_operation_to_text(
            spec=spec,
            current_text=current_text,
            existed_before=existed_before,
        )

        if op_result.changed:
            changed_ops.setdefault(spec.path, []).append(op_result)
            current_state[spec.path] = updated_text
            current_exists[spec.path] = True
            final_encoding[spec.path] = spec.encoding
            final_chmod[spec.path] = spec.chmod
            ensure_parent[spec.path] = ensure_parent.get(spec.path, False) or spec.ensure_parent

    outcomes: list[FileOutcome] = []
    for path, operations in changed_ops.items():
        existed_before, original_text, original_encoding = original_state[path]
        final_text = current_state[path]
        outcomes.append(
            FileOutcome(
                path=path,
                existed_before=existed_before,
                original_text=original_text,
                final_text=final_text,
                created=not existed_before,
                changed=(not existed_before) or (final_text != original_text),
                operations=operations,
                encoding=final_encoding.get(path, original_encoding),
                chmod=final_chmod.get(path),
                ensure_parent=ensure_parent.get(path, False),
            )
        )

    outcomes.sort(key=lambda item: str(item.path))
    return outcomes


def validate_expectations(outcomes: list[FileOutcome], args: argparse.Namespace) -> None:
    if args.fail_on_noop and not outcomes:
        raise SystemExit("no changes would be made")

    changed_op_total = sum(len(outcome.operations) for outcome in outcomes)
    changed_file_total = len(outcomes)

    if args.expect_total is not None and changed_op_total != args.expect_total:
        raise SystemExit(
            f"changed operations {changed_op_total}, expected {args.expect_total}"
        )

    if args.expect_files is not None and changed_file_total != args.expect_files:
        raise SystemExit(f"changed files {changed_file_total}, expected {args.expect_files}")

    if args.expect_per_file is not None:
        for outcome in outcomes:
            count = len(outcome.operations)
            if count != args.expect_per_file:
                raise SystemExit(
                    f"{outcome.path} has {count} changed operations, expected {args.expect_per_file}"
                )


def unified_diff(path: Path, original_text: str, final_text: str, context: int) -> str:
    original_lines = original_text.splitlines(keepends=True)
    final_lines = final_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        original_lines,
        final_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    return "".join(diff)


def describe_operation(result: OperationResult) -> str:
    details = [
        f"source={result.source}",
        f"mode={result.write_mode}",
        f"content={result.content_origin}",
        f"bytes_delta={result.bytes_delta}",
    ]
    if result.anchors_matched:
        details.append(f"anchors={result.anchors_matched}")
    if result.created_file:
        details.append("created=true")
    return ", ".join(details)


def print_preview(outcomes: list[FileOutcome], args: argparse.Namespace) -> None:
    if not outcomes:
        print("No changes.")
        return

    for index, outcome in enumerate(outcomes):
        if index:
            print("\n" + ("=" * 80) + "\n")
        changed_op_count = len(outcome.operations)
        bytes_delta = len(outcome.final_text) - len(outcome.original_text)
        print(
            f"# {outcome.path} | ops={changed_op_count} | created={str(outcome.created).lower()} "
            f"| bytes_delta={bytes_delta}"
        )
        for op_index, op in enumerate(outcome.operations, start=1):
            print(f"- op{op_index}: {describe_operation(op)}")
        print(unified_diff(outcome.path, outcome.original_text, outcome.final_text, args.context))


def apply_chmod(path: Path, chmod_value: str) -> None:
    if chmod_value == "+x":
        current_mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(current_mode | 0o111)
        return
    if chmod_value == "-x":
        current_mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(current_mode & ~0o111)
        return
    try:
        numeric_mode = int(chmod_value, 8)
    except ValueError as exc:
        raise SystemExit(f"invalid chmod value: {chmod_value!r}") from exc
    path.chmod(numeric_mode)


def apply_changes(outcomes: list[FileOutcome], args: argparse.Namespace) -> None:
    for outcome in outcomes:
        if outcome.ensure_parent:
            outcome.path.parent.mkdir(parents=True, exist_ok=True)

        if outcome.existed_before and args.backup:
            backup_path = Path(str(outcome.path) + args.backup_suffix)
            shutil.copyfile(outcome.path, backup_path)

        if not outcome.path.parent.exists():
            raise SystemExit(
                f"parent directory does not exist for {outcome.path}; use --ensure-parent"
            )

        write_text(outcome.path, outcome.final_text, outcome.encoding)
        if outcome.chmod:
            apply_chmod(outcome.path, outcome.chmod)


def emit_json_report(outcomes: list[FileOutcome]) -> None:
    payload = {
        "changed_files": len(outcomes),
        "changed_operations": sum(len(outcome.operations) for outcome in outcomes),
        "files": [
            {
                "path": str(outcome.path),
                "created": outcome.created,
                "changed": outcome.changed,
                "encoding": outcome.encoding,
                "operations": [
                    {
                        "source": op.source,
                        "write_mode": op.write_mode,
                        "content_origin": op.content_origin,
                        "bytes_delta": op.bytes_delta,
                        "anchors_matched": op.anchors_matched,
                        "created_file": op.created_file,
                    }
                    for op in outcome.operations
                ],
            }
            for outcome in outcomes
        ],
    }
    print(json.dumps(payload, indent=2))


def read_stdin_once(args: argparse.Namespace) -> str | None:
    if not args.stdin:
        return None
    return sys.stdin.read()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    if args.list_templates:
        list_templates()
        return 0

    stdin_text = read_stdin_once(args)
    specs = build_all_specs(args, stdin_text)

    if not specs:
        raise SystemExit("no operations requested")

    outcomes = process_operations(specs)
    validate_expectations(outcomes, args)

    if args.mode == "preview":
        print_preview(outcomes, args)
    else:
        apply_changes(outcomes, args)
        print(f"Applied changes to {len(outcomes)} file(s).")

    if args.json_report:
        emit_json_report(outcomes)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
