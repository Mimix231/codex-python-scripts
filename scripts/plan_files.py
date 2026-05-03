#!/usr/bin/env python3
"""
plan_files.py

Build a deterministic artifact graph for a coding request. Unlike the older
version, this planner is not limited to a few famous entrypoint files. It can
derive files from:
- the explicit prompt
- the current workspace manifests and stack
- architecture / spec / design markdown files in the workspace
- custom component/service/store/module names implied by the request
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Sequence

STACK_ORDER = [
    "tauri",
    "rust",
    "node",
    "typescript",
    "react",
    "vite",
    "tailwind",
    "shadcn",
    "python",
]

DOC_CANDIDATES = [
    "architecture.md",
    "README.md",
    "docs/architecture.md",
    "docs/spec.md",
    "docs/design.md",
    "SPEC.md",
    "DESIGN.md",
]

FILE_PATH_RE = re.compile(r"\b(?:src|app|docs|assets|public|engine|backend|frontend|src-tauri|lib)[\w./-]*\.[A-Za-z0-9]+\b", re.I)
NAMED_BLOCK_RE = re.compile(
    r"\b(?P<kind>component|page|route|screen|view|panel|dialog|modal|layout|store|hook|service|api|client|command|module|worker)\s+(?P<name>[A-Za-z][A-Za-z0-9_-]{1,60})\b",
    re.I,
)
QUOTED_NAME_RE = re.compile(r"[\"'`](?P<name>[A-Za-z][A-Za-z0-9 _-]{2,80})[\"'`]")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a deterministic artifact graph for a coding request."
    )
    parser.add_argument("--prompt", required=True, help="User request.")
    parser.add_argument("--workspace-root", default=".", help="Workspace root.")
    parser.add_argument("--json-report", action="store_true", help="Emit JSON report.")
    return parser.parse_args(argv)


def unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def to_pascal_case(value: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", value)
    return "".join(part[:1].upper() + part[1:] for part in parts if part)


def to_kebab_case(value: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", value)
    return "-".join(part.lower() for part in parts if part)


def detect_stack(root: Path, prompt: str) -> list[str]:
    prompt_lower = prompt.lower()
    stacks: list[str] = []
    if (root / "package.json").exists():
        stacks.append("node")
    if (root / "Cargo.toml").exists() or (root / "src-tauri" / "Cargo.toml").exists():
        stacks.extend(["rust"])
    if (root / "src-tauri").exists() or "tauri" in prompt_lower:
        stacks.append("tauri")
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        stacks.append("python")
    if any(token in prompt_lower for token in ["react", "tsx", "component", "page", "ui"]):
        stacks.append("react")
    if any(token in prompt_lower for token in ["typescript", "tsx", "tsconfig"]):
        stacks.append("typescript")
    if "vite" in prompt_lower:
        stacks.append("vite")
    if "tailwind" in prompt_lower:
        stacks.append("tailwind")
    if "shadcn" in prompt_lower:
        stacks.append("shadcn")

    ordered = unique(stacks)
    ordered.sort(key=lambda item: STACK_ORDER.index(item) if item in STACK_ORDER else len(STACK_ORDER))
    return ordered


def infer_project_type(prompt: str) -> str:
    prompt_lower = prompt.lower()
    if "game" in prompt_lower:
        return "game"
    if any(token in prompt_lower for token in ["desktop", "tauri"]):
        return "desktop_app"
    if any(token in prompt_lower for token in ["api", "backend", "server", "service"]):
        return "service"
    if any(token in prompt_lower for token in ["site", "frontend", "ui", "page", "react", "dashboard"]):
        return "web_app"
    if any(token in prompt_lower for token in ["architecture", "design doc", "spec"]):
        return "documentation"
    return "program"


def infer_validation_commands(stacks: list[str], root: Path, project_type: str) -> list[str]:
    if project_type == "documentation":
        return []
    commands: list[str] = []
    if any(stack in stacks for stack in ["node", "react", "vite", "typescript"]) and (root / "package.json").exists():
        commands.append("npm run build")
    if any(stack in stacks for stack in ["rust", "tauri"]) and ((root / "Cargo.toml").exists() or (root / "src-tauri" / "Cargo.toml").exists()):
        commands.append("cargo check")
    if "python" in stacks and (root / "pyproject.toml").exists():
        commands.append("python -m compileall .")
    return unique(commands)


def mentioned_file_paths(text: str) -> list[str]:
    return unique(match.group(0).replace("\\", "/") for match in FILE_PATH_RE.finditer(text))


def discover_architecture_sources(root: Path, prompt: str) -> list[Path]:
    explicit = [root / path for path in mentioned_file_paths(prompt) if path.lower().endswith((".md", ".rst", ".txt"))]
    candidates = [root / path for path in DOC_CANDIDATES]
    sources = [path for path in [*explicit, *candidates] if path.exists() and path.is_file()]
    return unique_paths(sources)


def unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    ordered: list[Path] = []
    for path in paths:
        normalized = path.resolve().as_posix().lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(path)
    return ordered


def read_text_safe(path: Path, max_chars: int = 32000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except OSError:
        return ""


def build_artifact_node(
    path: str,
    root: Path,
    *,
    role: str,
    reason: str,
    source: str,
    operation: str | None = None,
    blueprint_role: str | None = None,
    sections: list[str] | None = None,
    required_symbols: list[str] | None = None,
    completion_checks: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    normalized = path.replace("\\", "/").lstrip("./")
    absolute = root / normalized
    language = infer_language(Path(normalized))
    next_operation = operation or ("edit" if absolute.exists() else "create")
    return {
        "path": normalized,
        "operation": next_operation,
        "role": role,
        "language": language,
        "blueprintRole": blueprint_role,
        "reason": reason,
        "source": source,
        "dependsOn": unique(depends_on or []),
        "sections": unique(sections or []),
        "requiredSymbols": unique(required_symbols or []),
        "completionChecks": unique(completion_checks or []),
    }


def infer_language(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
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
        ".yml": "yaml",
        ".yaml": "yaml",
    }.get(suffix, "text")


def infer_default_artifacts(stacks: list[str], project_type: str, root: Path) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = []
    if project_type == "documentation":
        return nodes

    if "tauri" in stacks:
        nodes.extend(
            [
                build_artifact_node(
                    "package.json",
                    root,
                    role="config",
                    reason="Project manifest for the frontend/runtime package.",
                    source="stack",
                    blueprint_role="package-manifest",
                ),
                build_artifact_node(
                    "src/main.tsx",
                    root,
                    role="source",
                    reason="Frontend entrypoint required by a Tauri React application.",
                    source="stack",
                    blueprint_role="react-entrypoint",
                    sections=["imports", "root mount", "render App"],
                    completion_checks=["Renders app"],
                ),
                build_artifact_node(
                    "src/App.tsx",
                    root,
                    role="source",
                    reason="Top-level app shell for the desktop UI.",
                    source="stack",
                    blueprint_role="react-app-shell",
                    sections=["imports", "layout structure", "state/hooks", "feature composition", "export App"],
                    required_symbols=["App"],
                    completion_checks=["App component exists", "The app shell renders meaningful UI"],
                ),
                build_artifact_node(
                    "src-tauri/Cargo.toml",
                    root,
                    role="config",
                    reason="Rust manifest for the Tauri backend.",
                    source="stack",
                    blueprint_role="cargo-manifest",
                ),
                build_artifact_node(
                    "src-tauri/src/main.rs",
                    root,
                    role="source",
                    reason="Rust backend entrypoint for the desktop app.",
                    source="stack",
                    blueprint_role="rust-main",
                    sections=["imports", "state/types", "commands", "runtime wiring"],
                    completion_checks=["Contains an entry point or exposed commands"],
                ),
            ]
        )
    elif any(stack in stacks for stack in ["react", "node", "typescript", "vite"]):
        nodes.extend(
            [
                build_artifact_node(
                    "package.json",
                    root,
                    role="config",
                    reason="Node package manifest for the web application.",
                    source="stack",
                    blueprint_role="package-manifest",
                ),
                build_artifact_node(
                    "src/main.tsx" if "react" in stacks else "src/main.ts",
                    root,
                    role="source",
                    reason="Frontend entrypoint for the application.",
                    source="stack",
                    blueprint_role="react-entrypoint" if "react" in stacks else "service-module",
                    completion_checks=["Renders app"] if "react" in stacks else [],
                )
            ]
        )
        if "react" in stacks:
            nodes.append(
                build_artifact_node(
                    "src/App.tsx",
                    root,
                    role="source",
                    reason="Top-level app shell for the frontend.",
                    source="stack",
                    blueprint_role="react-app-shell",
                    required_symbols=["App"],
                    completion_checks=["App component exists"],
                )
            )
    elif "rust" in stacks:
        nodes.extend(
            [
                build_artifact_node("Cargo.toml", root, role="config", reason="Rust package manifest.", source="stack", blueprint_role="cargo-manifest"),
                build_artifact_node("src/main.rs", root, role="source", reason="Rust application entrypoint.", source="stack", blueprint_role="rust-main"),
            ]
        )
    elif "python" in stacks:
        nodes.extend(
            [
                build_artifact_node("pyproject.toml", root, role="config", reason="Python project manifest.", source="stack", blueprint_role="toml-config"),
                build_artifact_node("main.py", root, role="source", reason="Python application entrypoint.", source="stack", blueprint_role="python-entrypoint"),
            ]
        )

    return nodes


def derive_path_from_kind(kind: str, name: str, stacks: list[str]) -> tuple[str, str]:
    normalized_kind = kind.lower()
    pascal = to_pascal_case(name)
    kebab = to_kebab_case(name)
    if normalized_kind in {"component", "panel", "dialog", "modal", "layout", "view"}:
        return (f"src/components/{pascal}.tsx", "react-component")
    if normalized_kind in {"page", "route", "screen"}:
        return (f"src/pages/{pascal}.tsx", "react-page")
    if normalized_kind == "hook":
        hook_name = pascal if pascal.startswith("Use") else f"Use{pascal}"
        return (f"src/hooks/{hook_name[:1].lower() + hook_name[1:]}.ts", "hook-module")
    if normalized_kind == "store":
        return (f"src/stores/{kebab}.ts", "state-store")
    if normalized_kind in {"service", "api", "client", "worker"}:
        return (f"src/lib/{kebab}.ts", "service-module")
    if normalized_kind in {"command", "module"} and any(stack in stacks for stack in ["tauri", "rust"]):
        return (f"src-tauri/src/{kebab}.rs", "rust-command-module")
    if normalized_kind in {"command", "module"}:
        return (f"src/lib/{kebab}.ts", "service-module")
    return (f"src/lib/{kebab}.ts", "service-module")


def infer_named_artifacts(text: str, root: Path, stacks: list[str], source: str) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = []
    for match in NAMED_BLOCK_RE.finditer(text):
        kind = match.group("kind")
        name = match.group("name")
        path, blueprint_role = derive_path_from_kind(kind, name, stacks)
        role = "source" if not path.endswith((".json", ".toml", ".md")) else "config"
        sections = [f"{kind} contract", f"{kind} implementation", f"{kind} integration"]
        required_symbols = [to_pascal_case(name)] if blueprint_role in {"react-component", "react-page"} else []
        nodes.append(
            build_artifact_node(
                path,
                root,
                role=role,
                reason=f"{kind.capitalize()} '{name}' is explicitly mentioned in the request or architecture.",
                source=source,
                blueprint_role=blueprint_role,
                sections=sections,
                required_symbols=required_symbols,
                completion_checks=["The file exports meaningful logic or UI for the requested feature."],
            )
        )
    return nodes


def infer_custom_ui_artifacts(text: str, root: Path, stacks: list[str], source: str) -> list[dict[str, object]]:
    if not any(stack in stacks for stack in ["react", "node", "typescript", "vite", "tauri"]):
        return []

    tokens = [
        ("sidebar", "Sidebar"),
        ("toolbar", "Toolbar"),
        ("canvas", "Canvas"),
        ("editor", "EditorShell"),
        ("settings", "SettingsPanel"),
        ("gallery", "GalleryPanel"),
        ("timeline", "TimelinePanel"),
        ("prompt panel", "PromptPanel"),
    ]
    nodes: list[dict[str, object]] = []
    lower = text.lower()
    for needle, component_name in tokens:
        if needle not in lower:
            continue
        path = f"src/components/{component_name}.tsx"
        nodes.append(
            build_artifact_node(
                path,
                root,
                role="source",
                reason=f"The request or architecture implies a {needle} UI surface.",
                source=source,
                blueprint_role="react-component",
                sections=["imports", "props/types", "component markup", "handlers", "exports"],
                required_symbols=[component_name],
                completion_checks=["The file exports a component"],
            )
        )
    return nodes


def infer_explicit_path_artifacts(text: str, root: Path, source: str) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = []
    for path in mentioned_file_paths(text):
        suffix = Path(path).suffix.lower()
        role = "doc" if suffix in {".md", ".rst", ".txt"} else "config" if suffix in {".json", ".toml", ".yaml", ".yml"} else "source"
        nodes.append(
            build_artifact_node(
                path,
                root,
                role=role,
                reason=f"{path} is explicitly mentioned.",
                source=source,
            )
        )
    return nodes


def parse_architecture_artifacts(root: Path, prompt: str, stacks: list[str]) -> tuple[list[dict[str, object]], list[str]]:
    nodes: list[dict[str, object]] = []
    notes: list[str] = []
    for source_path in discover_architecture_sources(root, prompt):
        content = read_text_safe(source_path)
        if not content.strip():
            continue
        notes.append(f"Parsed architecture/spec source: {source_path.relative_to(root).as_posix()}")
        nodes.extend(infer_explicit_path_artifacts(content, root, "architecture"))
        nodes.extend(infer_named_artifacts(content, root, stacks, "architecture"))
        nodes.extend(infer_custom_ui_artifacts(content, root, stacks, "architecture"))
    return dedupe_artifacts(nodes, root), notes


def dedupe_artifacts(nodes: list[dict[str, object]], root: Path) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for node in nodes:
        path = str(node["path"]).replace("\\", "/")
        existing = merged.get(path)
        if not existing:
            merged[path] = node
            continue
        for key in ["dependsOn", "sections", "requiredSymbols", "completionChecks"]:
            existing_values = list(existing.get(key, []))
            new_values = list(node.get(key, []))
            existing[key] = unique([*existing_values, *new_values])
        if existing.get("source") != "prompt" and node.get("source") == "prompt":
            existing["source"] = "prompt"
            existing["reason"] = node.get("reason", existing.get("reason"))
        if existing.get("operation") == "inspect" and node.get("operation") in {"create", "edit", "patch"}:
            existing["operation"] = node["operation"]
        if not existing.get("blueprintRole") and node.get("blueprintRole"):
            existing["blueprintRole"] = node["blueprintRole"]
    for path, node in list(merged.items()):
        node["operation"] = node.get("operation") or ("edit" if (root / path).exists() else "create")
    return list(merged.values())


def build_plan(root: Path, prompt: str) -> dict[str, object]:
    stacks = detect_stack(root, prompt)
    project_type = infer_project_type(prompt)
    notes = [
        f"Detected stacks: {', '.join(stacks) if stacks else 'none'}",
        f"Detected project type: {project_type}",
    ]

    nodes = []
    nodes.extend(infer_default_artifacts(stacks, project_type, root))
    nodes.extend(infer_explicit_path_artifacts(prompt, root, "prompt"))
    nodes.extend(infer_named_artifacts(prompt, root, stacks, "prompt"))
    nodes.extend(infer_custom_ui_artifacts(prompt, root, stacks, "prompt"))
    architecture_nodes, architecture_notes = parse_architecture_artifacts(root, prompt, stacks)
    nodes.extend(architecture_nodes)
    notes.extend(architecture_notes)

    deduped_nodes = dedupe_artifacts(nodes, root)
    files_to_create = [node["path"] for node in deduped_nodes if node.get("operation") == "create"]
    files_to_edit = [node["path"] for node in deduped_nodes if node.get("operation") in {"edit", "patch"}]
    files_to_inspect = [node["path"] for node in deduped_nodes if node.get("operation") == "inspect"]

    return {
        "prompt": prompt,
        "workspaceRoot": str(root),
        "projectType": project_type,
        "stack": stacks,
        "filesToCreate": unique(files_to_create),
        "filesToEdit": unique(files_to_edit),
        "filesToInspect": unique(files_to_inspect),
        "artifactGraph": deduped_nodes,
        "validationCommands": infer_validation_commands(stacks, root, project_type),
        "notes": notes,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = Path(args.workspace_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"workspace root does not exist: {root}")

    report = build_plan(root, args.prompt)
    print(
        f"Planned {len(report['artifactGraph'])} artifacts for {report['projectType']} "
        f"({', '.join(report['stack']) if report['stack'] else 'unknown stack'})."
    )
    if report["filesToInspect"]:
        print(f"filesToInspect: {', '.join(report['filesToInspect'])}")
    if report["filesToCreate"]:
        print(f"filesToCreate: {', '.join(report['filesToCreate'])}")
    if report["filesToEdit"]:
        print(f"filesToEdit: {', '.join(report['filesToEdit'])}")
    if args.json_report:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
