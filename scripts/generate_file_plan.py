#!/usr/bin/env python3
"""
generate_file_plan.py

Produce a richer per-file implementation plan. The output is no longer a flat
"purpose + sections" stub; it now includes:
- blueprint role
- operation hint
- section graph
- required symbols
- completion checks
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

LANGUAGE_MAP = {
    ".py": "python",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
    ".css": "css",
    ".html": "html",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic plan for one file."
    )
    parser.add_argument("--path", required=True, help="Target file path.")
    parser.add_argument("--prompt", required=True, help="User request.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root.")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON report.")
    return parser.parse_args(argv)


def infer_language(path: Path) -> str:
    return LANGUAGE_MAP.get(path.suffix.lower(), "text")


def infer_blueprint_role(path: str) -> str:
    normalized = path.replace("\\", "/").lower()
    if normalized in {"src/app.tsx", "src/app/app.tsx"}:
        return "react-app-shell"
    if normalized.endswith(("src/main.tsx", "src/main.ts", "src/index.tsx")):
        return "react-entrypoint"
    if "/components/" in normalized or "/ui/" in normalized or normalized.endswith(".tsx"):
        return "react-component"
    if "/pages/" in normalized or "/routes/" in normalized:
        return "react-page"
    if "/hooks/" in normalized:
        return "hook-module"
    if "/stores/" in normalized or "/state/" in normalized:
        return "state-store"
    if normalized.endswith("package.json"):
        return "package-manifest"
    if normalized.endswith("cargo.toml"):
        return "cargo-manifest"
    if normalized.endswith(("tauri.conf.json", "src-tauri/tauri.conf.json")):
        return "tauri-config"
    if normalized.endswith(".rs"):
        return "rust-command-module"
    if normalized.endswith(".md"):
        return "markdown-architecture"
    if normalized.endswith(".json"):
        return "json-config"
    if normalized.endswith(".toml"):
        return "toml-config"
    if normalized.endswith(".py"):
        return "python-entrypoint"
    return "service-module"


def infer_operation_hint(path: Path, workspace_root: Path) -> str:
    absolute = workspace_root / path
    if not absolute.exists():
        return "create"
    if path.suffix.lower() in {".tsx", ".ts", ".jsx", ".js", ".rs", ".py"}:
        return "patch"
    return "edit"


def infer_sections(path: Path, blueprint_role: str) -> list[str]:
    by_role = {
        "react-app-shell": ["imports", "layout structure", "state/hooks", "feature composition", "export App"],
        "react-entrypoint": ["imports", "root mount", "global styles", "render App"],
        "react-component": ["imports", "props/types", "state/hooks", "component markup", "handlers", "exports"],
        "react-page": ["imports", "page state", "layout", "actions", "exports"],
        "hook-module": ["imports", "types", "hook state", "hook logic", "exports"],
        "state-store": ["imports", "state shape", "actions", "exports"],
        "service-module": ["imports", "types", "helpers", "service logic", "exports"],
        "package-manifest": ["package metadata", "scripts", "dependencies", "devDependencies"],
        "cargo-manifest": ["package metadata", "dependencies", "workspace configuration"],
        "tauri-config": ["app metadata", "window configuration", "runtime settings"],
        "rust-command-module": ["imports", "types", "command handlers", "helpers", "tests"],
        "markdown-architecture": ["title", "overview", "system architecture", "flows", "implementation notes"],
        "json-config": ["top-level keys", "required fields", "runtime options"],
        "toml-config": ["package metadata", "dependencies", "tool configuration"],
        "python-entrypoint": ["imports", "configuration", "core logic", "entrypoint"],
    }
    if blueprint_role in by_role:
        return by_role[blueprint_role]

    language = infer_language(path)
    fallback = {
        "python": ["imports", "constants", "core logic", "entrypoint"],
        "rust": ["imports", "types", "functions", "wiring", "tests"],
        "tsx": ["imports", "types", "state", "markup", "handlers", "exports"],
        "typescript": ["imports", "types", "helpers", "main exports"],
        "javascript": ["imports", "helpers", "main logic", "exports"],
        "json": ["top-level keys", "required fields"],
        "toml": ["package metadata", "dependencies"],
        "markdown": ["title", "overview", "details"],
    }
    return fallback.get(language, ["header", "body", "footer"])


def infer_required_symbols(path: Path, blueprint_role: str) -> list[str]:
    stem = path.stem
    if blueprint_role in {"react-app-shell"}:
        return ["App"]
    if blueprint_role in {"react-component", "react-page"}:
        return [stem[:1].upper() + stem[1:]]
    if blueprint_role == "hook-module":
        return [stem if stem.startswith("use") else f"use{stem[:1].upper() + stem[1:]}"]
    return []


def infer_completion_checks(path: Path, blueprint_role: str) -> list[str]:
    checks = {
        "react-app-shell": ["App component exists", "The app shell renders meaningful UI"],
        "react-entrypoint": ["Renders app"],
        "react-component": ["The file exports a component"],
        "react-page": ["The file exports a component"],
        "hook-module": ["The file exports a hook"],
        "service-module": ["The file exports meaningful logic"],
        "package-manifest": ["Scripts and dependency blocks are present"],
        "cargo-manifest": ["The file contains valid TOML"],
        "tauri-config": ["The file contains valid JSON"],
        "rust-command-module": ["Contains an entry point or exposed commands"],
        "markdown-architecture": ["System architecture section is present", "Overview section is present"],
        "json-config": ["The file contains valid JSON"],
        "toml-config": ["The file contains valid TOML"],
        "python-entrypoint": ["The file contains executable Python code"],
    }
    return checks.get(blueprint_role, [f"{path.name} is complete and valid for its file role"])


def infer_purpose(path: Path, prompt: str, blueprint_role: str) -> str:
    if blueprint_role == "markdown-architecture":
        return "Architecture or documentation file that should reflect the requested system design."
    if blueprint_role in {"react-app-shell", "react-component", "react-page"}:
        return f"UI artifact for request: {prompt}"
    if blueprint_role in {"service-module", "hook-module", "state-store"}:
        return f"Logic module that supports the requested feature: {prompt}"
    if blueprint_role in {"cargo-manifest", "package-manifest", "json-config", "toml-config", "tauri-config"}:
        return f"Configuration or manifest file required by the requested architecture: {prompt}"
    if blueprint_role == "rust-command-module":
        return f"Rust backend file required for request: {prompt}"
    if blueprint_role == "python-entrypoint":
        return f"Python implementation file for request: {prompt}"
    return f"Implementation file for request: {prompt}"


def infer_dependencies(path: Path, workspace_root: Path, blueprint_role: str) -> list[str]:
    dependencies: list[str] = []
    normalized = path.as_posix().lower()
    if normalized.endswith((".ts", ".tsx", ".js", ".jsx")) and (workspace_root / "package.json").exists():
        dependencies.append("package.json")
    if normalized.endswith(".rs") and ((workspace_root / "Cargo.toml").exists() or (workspace_root / "src-tauri" / "Cargo.toml").exists()):
        dependencies.append("src-tauri/Cargo.toml" if (workspace_root / "src-tauri" / "Cargo.toml").exists() else "Cargo.toml")
    if normalized.endswith(".py") and (workspace_root / "pyproject.toml").exists():
        dependencies.append("pyproject.toml")
    if blueprint_role == "react-entrypoint":
        dependencies.append("src/App.tsx")
    return sorted(set(dependencies))


def infer_validation_command(path: Path, blueprint_role: str) -> str | None:
    language = infer_language(path)
    if blueprint_role in {"package-manifest", "react-app-shell", "react-entrypoint", "react-component", "react-page", "service-module", "hook-module", "state-store"}:
        return "npm run build"
    if blueprint_role in {"cargo-manifest", "rust-command-module"} or language == "rust":
        return "cargo check"
    if blueprint_role == "python-entrypoint" or language == "python":
        return "python -m compileall ."
    return None


def build_report(path_text: str, prompt: str, workspace_root: Path) -> dict[str, object]:
    path = Path(path_text)
    absolute = workspace_root / path
    blueprint_role = infer_blueprint_role(path.as_posix())
    return {
        "path": path.as_posix(),
        "exists": absolute.exists(),
        "language": infer_language(path),
        "purpose": infer_purpose(path, prompt, blueprint_role),
        "sections": infer_sections(path, blueprint_role),
        "dependencies": infer_dependencies(path, workspace_root, blueprint_role),
        "validationCommand": infer_validation_command(path, blueprint_role),
        "shouldReadFirst": absolute.exists(),
        "blueprintRole": blueprint_role,
        "operationHint": infer_operation_hint(path, workspace_root),
        "requiredSymbols": infer_required_symbols(path, blueprint_role),
        "completionChecks": infer_completion_checks(path, blueprint_role),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    report = build_report(args.path, args.prompt, root)

    print(f"File plan: {report['path']}")
    print(f"Blueprint: {report['blueprintRole']}")
    print(f"Language: {report['language']}")
    print(f"Operation: {report['operationHint']}")
    print(f"Sections: {', '.join(report['sections'])}")

    if args.json_report:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
