#!/usr/bin/env python3
"""
scaffold_module.py

Generate common module/file layouts and delegate the actual writes to
write_file.py.

This keeps scaffolding deterministic while reusing the repository's preview-
first writer instead of duplicating write logic again.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(slots=True)
class GeneratedFile:
    path: Path
    content: str
    chmod: str | None = None


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scaffold common modules and scripts by delegating writes to write_file.py."
    )
    parser.add_argument(
        "--kind",
        choices=[
            "c-pair",
            "cpp-pair",
            "python-module",
            "lua-module",
            "markdown-doc",
            "shell-script",
            "powershell-script",
            "json-config",
            "toml-config",
            "ini-config",
        ],
        required=True,
        help="What kind of scaffold to generate.",
    )
    parser.add_argument("--name", required=True, help="Base module or file name.")
    parser.add_argument("--dir", default=".", help="Output directory. Default: current directory")
    parser.add_argument("--namespace", default="generated", help="Namespace for C++ scaffolds.")
    parser.add_argument("--summary", default="", help="Short summary text for docs/scripts.")
    parser.add_argument("--body", default="", help="Optional body text inserted into generated files.")
    parser.add_argument("--body-file", help="Read body text from a file.")
    parser.add_argument(
        "--package",
        action="store_true",
        help="For python-module, generate a package directory with __init__.py instead of a single module file.",
    )
    parser.add_argument(
        "--with-readme",
        action="store_true",
        help="Generate a README.md alongside the scaffold.",
    )
    parser.add_argument(
        "--with-test",
        action="store_true",
        help="Generate a basic test file in --test-dir.",
    )
    parser.add_argument("--test-dir", default="tests", help="Test directory for --with-test. Default: tests")
    parser.add_argument(
        "--mode",
        choices=["preview", "apply"],
        default="preview",
        help="Preview diffs or apply changes.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing files.")
    parser.add_argument("--encoding", default="utf-8", help="Text encoding for generated files.")
    parser.add_argument(
        "--newline",
        choices=["keep", "lf", "crlf"],
        default="lf",
        help="Newline style passed through to write_file.py. Default: lf",
    )
    parser.add_argument(
        "--json-report",
        action="store_true",
        help="Emit a JSON summary after running write_file.py for all generated files.",
    )
    return parser.parse_args(argv)


def read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        if args.body:
            raise SystemExit("use only one of --body or --body-file")
        return Path(args.body_file).read_text(encoding=args.encoding)
    return args.body


def include_guard_for(path: Path) -> str:
    text = str(path).replace("\\", "/").upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text).strip("_")
    return text + "_H"


def snake_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "module"


def make_readme(name: str, summary: str) -> str:
    title = name.replace("_", " ").replace("-", " ").strip()
    body = summary or "Describe this module here."
    return f"# {title}\n\n{body}\n"


def generate_c_pair(base_dir: Path, name: str, summary: str, body: str) -> list[GeneratedFile]:
    stem = snake_name(name)
    header_path = base_dir / f"{stem}.h"
    source_path = base_dir / f"{stem}.c"
    guard = include_guard_for(header_path)
    function_name = f"{stem}_init"
    header_body = body or f"void {function_name}(void);"
    source_body = body or (
        f"void {function_name}(void) {{\n"
        f"    // TODO: implement {function_name}.\n"
        f"}}"
    )
    header = (
        f"#ifndef {guard}\n"
        f"#define {guard}\n\n"
        f"{header_body}\n\n"
        f"#endif /* {guard} */\n"
    )
    source = (
        f"#include \"{stem}.h\"\n\n"
        f"{source_body}\n"
    )
    return [GeneratedFile(header_path, header), GeneratedFile(source_path, source)]


def generate_cpp_pair(base_dir: Path, name: str, namespace: str, summary: str, body: str) -> list[GeneratedFile]:
    stem = snake_name(name)
    header_path = base_dir / f"{stem}.h"
    source_path = base_dir / f"{stem}.cpp"
    guard = include_guard_for(header_path)
    function_name = f"{stem}_init"
    header_body = body or f"void {function_name}();"
    source_body = body or (
        f"void {function_name}() {{\n"
        f"    // TODO: implement {function_name}.\n"
        f"}}"
    )
    header = (
        f"#ifndef {guard}\n"
        f"#define {guard}\n\n"
        f"namespace {namespace} {{\n\n"
        f"{header_body}\n\n"
        f"}}  // namespace {namespace}\n\n"
        f"#endif /* {guard} */\n"
    )
    source = (
        f"#include \"{stem}.h\"\n\n"
        f"namespace {namespace} {{\n\n"
        f"{source_body}\n\n"
        f"}}  // namespace {namespace}\n"
    )
    return [GeneratedFile(header_path, header), GeneratedFile(source_path, source)]


def generate_python_module(base_dir: Path, name: str, summary: str, body: str, package: bool) -> list[GeneratedFile]:
    stem = snake_name(name)
    content = (
        f'"""{summary or stem}"""\n\n'
        f"from __future__ import annotations\n\n"
        f"{body or 'def main() -> int:\n    return 0'}\n"
    )
    if package:
        return [GeneratedFile(base_dir / stem / "__init__.py", content)]
    return [GeneratedFile(base_dir / f"{stem}.py", content)]


def generate_lua_module(base_dir: Path, name: str, summary: str, body: str) -> list[GeneratedFile]:
    stem = snake_name(name)
    content = (
        "local M = {}\n\n"
        f"-- {summary or stem}\n"
        f"{body or 'function M.main()\nend'}\n\n"
        "return M\n"
    )
    return [GeneratedFile(base_dir / f"{stem}.lua", content)]


def generate_markdown_doc(base_dir: Path, name: str, summary: str, body: str) -> list[GeneratedFile]:
    stem = snake_name(name)
    title = name.replace("_", " ").replace("-", " ").strip()
    content = f"# {title}\n\n{summary or body or 'TODO'}\n"
    return [GeneratedFile(base_dir / f"{stem}.md", content)]


def generate_shell_script(base_dir: Path, name: str, summary: str, body: str) -> list[GeneratedFile]:
    stem = snake_name(name)
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"# {summary or stem}\n"
        f"{body or 'echo \"TODO\"'}\n"
    )
    return [GeneratedFile(base_dir / f"{stem}.sh", content, chmod="+x")]


def generate_powershell_script(base_dir: Path, name: str, summary: str, body: str) -> list[GeneratedFile]:
    stem = snake_name(name)
    content = (
        "param()\n"
        "Set-StrictMode -Version Latest\n"
        '$ErrorActionPreference = "Stop"\n\n'
        f"# {summary or stem}\n"
        f"{body or 'Write-Host \"TODO\"'}\n"
    )
    return [GeneratedFile(base_dir / f"{stem}.ps1", content)]


def generate_json_config(base_dir: Path, name: str, summary: str, body: str) -> list[GeneratedFile]:
    stem = snake_name(name)
    payload = {"name": stem, "summary": summary, "body": body}
    return [GeneratedFile(base_dir / f"{stem}.json", json.dumps(payload, indent=2) + "\n")]


def generate_toml_config(base_dir: Path, name: str, summary: str, body: str) -> list[GeneratedFile]:
    stem = snake_name(name)
    content = f'name = "{stem}"\nsummary = "{summary}"\nbody = "{body}"\n'
    return [GeneratedFile(base_dir / f"{stem}.toml", content)]


def generate_ini_config(base_dir: Path, name: str, summary: str, body: str) -> list[GeneratedFile]:
    stem = snake_name(name)
    content = f"[{stem}]\nsummary = {summary}\nbody = {body}\n"
    return [GeneratedFile(base_dir / f"{stem}.ini", content)]


def generate_test_files(base_dir: Path, name: str, kind: str) -> list[GeneratedFile]:
    stem = snake_name(name)
    if kind == "c-pair":
        content = f"#include \"{stem}.h\"\n\nint main(void) {{\n    {stem}_init();\n    return 0;\n}}\n"
        return [GeneratedFile(base_dir / f"test_{stem}.c", content)]
    if kind == "cpp-pair":
        content = f"#include \"{stem}.h\"\n\nint main() {{\n    {stem}_init();\n    return 0;\n}}\n"
        return [GeneratedFile(base_dir / f"test_{stem}.cpp", content)]
    if kind == "python-module":
        content = f"from {stem} import main\n\n\ndef test_main() -> None:\n    assert main() == 0\n"
        return [GeneratedFile(base_dir / f"test_{stem}.py", content)]
    if kind == "lua-module":
        content = f"local m = require('{stem}')\n\nassert(type(m) == 'table')\n"
        return [GeneratedFile(base_dir / f"{stem}_spec.lua", content)]
    return []


def build_files(args: argparse.Namespace) -> list[GeneratedFile]:
    base_dir = Path(args.dir)
    body = read_body(args)
    files: list[GeneratedFile]

    if args.kind == "c-pair":
        files = generate_c_pair(base_dir, args.name, args.summary, body)
    elif args.kind == "cpp-pair":
        files = generate_cpp_pair(base_dir, args.name, args.namespace, args.summary, body)
    elif args.kind == "python-module":
        files = generate_python_module(base_dir, args.name, args.summary, body, args.package)
    elif args.kind == "lua-module":
        files = generate_lua_module(base_dir, args.name, args.summary, body)
    elif args.kind == "markdown-doc":
        files = generate_markdown_doc(base_dir, args.name, args.summary, body)
    elif args.kind == "shell-script":
        files = generate_shell_script(base_dir, args.name, args.summary, body)
    elif args.kind == "powershell-script":
        files = generate_powershell_script(base_dir, args.name, args.summary, body)
    elif args.kind == "json-config":
        files = generate_json_config(base_dir, args.name, args.summary, body)
    elif args.kind == "toml-config":
        files = generate_toml_config(base_dir, args.name, args.summary, body)
    elif args.kind == "ini-config":
        files = generate_ini_config(base_dir, args.name, args.summary, body)
    else:
        raise SystemExit(f"unsupported kind: {args.kind}")

    if args.with_readme:
        files.append(GeneratedFile(base_dir / "README.md", make_readme(args.name, args.summary)))
    if args.with_test:
        files.extend(generate_test_files(base_dir / args.test_dir, args.name, args.kind))
    return files


def run_writer(file: GeneratedFile, args: argparse.Namespace) -> subprocess.CompletedProcess[str]:
    writer_path = Path(__file__).with_name("write_file.py")
    command = [
        sys.executable,
        str(writer_path),
        "--path",
        str(file.path),
        "--stdin",
        "--write-mode",
        "create",
        "--mode",
        args.mode,
        "--encoding",
        args.encoding,
        "--newline",
        args.newline,
        "--ensure-parent",
    ]
    if args.force:
        command.append("--force")
    if file.chmod:
        command.extend(["--chmod", file.chmod])

    return subprocess.run(
        command,
        input=file.content,
        text=True,
        capture_output=True,
        check=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    files = build_files(args)
    results: list[dict[str, object]] = []

    for file in files:
        completed = run_writer(file, args)
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        if completed.returncode != 0:
            return completed.returncode
        results.append({"path": str(file.path), "chmod": file.chmod})

    if args.json_report:
        print(json.dumps({"generated_files": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
