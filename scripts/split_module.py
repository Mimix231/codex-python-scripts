#!/usr/bin/env python3
"""
split_module.py

Refactor helper for splitting a source module into one or more target modules.

The tool is intentionally deterministic. It does not try to "understand" an
entire file the way an IDE refactor engine would, but it handles the common
split workflow well enough to be useful for very large files:

- TypeScript / TSX / JavaScript / JSX:
  - prefers parsing with the local TypeScript compiler via `node`
  - falls back to a Python scanner if `node` or `typescript` is unavailable
- Python:
  - uses the built-in `ast` module
- Supports selecting content by:
  - exact symbol names (`--symbols`)
  - best-effort intent queries (`--what-should-be-split`)
  - explicit marker regions:
      // @split-start Name
      // @split-end Name
      # @split-start Name
      # @split-end Name
- Supports `preview`, `copy`, and `move` modes
- Can pull in local top-level dependencies
- Can carry forward only the imports used by the extracted content
- Can insert an import back into the source file after a move

This tool is designed to be safe:
- preview is the default mode
- it prints warnings for unresolved identifiers instead of pretending they do
  not exist
- it does not overwrite files unless `--force` is used

Examples:

    python tools/split_module.py ^
      --filename src/components/Chat.tsx ^
      --save-as src/components/chat-shell/WorkbenchTitlebar.tsx ^
      --symbols WorkbenchTitlebar ^
      --mode move ^
      --insert-source-import

    python tools/split_module.py ^
      --filename src/components/Chat.tsx ^
      --save-as src/components/chat-runtime/runtimeHelpers.ts ^
      --what-should-be-split "ollama helpers, prompt builders" ^
      --include-local-deps ^
      --mode preview
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import keyword
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_MARKER_START = "@split-start"
DEFAULT_MARKER_END = "@split-end"
DEFAULT_MODE = "preview"

JS_LIKE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
PYTHON_EXTENSIONS = {".py"}

JS_KEYWORDS = {
    "abstract",
    "any",
    "as",
    "async",
    "await",
    "boolean",
    "break",
    "case",
    "catch",
    "class",
    "const",
    "constructor",
    "continue",
    "debugger",
    "declare",
    "default",
    "delete",
    "do",
    "else",
    "enum",
    "export",
    "extends",
    "false",
    "finally",
    "for",
    "from",
    "function",
    "get",
    "if",
    "implements",
    "import",
    "in",
    "instanceof",
    "interface",
    "is",
    "keyof",
    "let",
    "module",
    "namespace",
    "never",
    "new",
    "null",
    "number",
    "object",
    "of",
    "package",
    "private",
    "protected",
    "public",
    "readonly",
    "require",
    "return",
    "satisfies",
    "set",
    "static",
    "string",
    "super",
    "switch",
    "symbol",
    "this",
    "throw",
    "true",
    "try",
    "type",
    "typeof",
    "undefined",
    "unique",
    "unknown",
    "var",
    "void",
    "while",
    "with",
    "yield",
}

COMMON_JS_GLOBALS = {
    "Array",
    "BigInt",
    "Boolean",
    "Date",
    "Error",
    "JSON",
    "Map",
    "Math",
    "Number",
    "Object",
    "Promise",
    "Reflect",
    "RegExp",
    "Set",
    "String",
    "Symbol",
    "URL",
    "URLSearchParams",
    "WeakMap",
    "WeakSet",
    "console",
    "document",
    "global",
    "globalThis",
    "history",
    "Intl",
    "location",
    "module",
    "navigator",
    "performance",
    "process",
    "queueMicrotask",
    "requestAnimationFrame",
    "require",
    "screen",
    "setInterval",
    "setTimeout",
    "structuredClone",
    "window",
}

COMMON_PYTHON_GLOBALS = set(dir(__builtins__)) | {"__name__", "__file__", "__package__"}


@dataclass(slots=True)
class ImportStatement:
    text: str
    start_line: int
    end_line: int
    local_names: list[str] = field(default_factory=list)
    module: str | None = None
    side_effect_only: bool = False


@dataclass(slots=True)
class SymbolBlock:
    name: str
    kind: str
    start_line: int
    end_line: int
    code: str
    exported: bool = False
    default_export: bool = False
    source: str = "symbol"
    matched_by: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ModuleParseResult:
    language: str
    path: Path
    text: str
    lines: list[str]
    imports: list[ImportStatement]
    symbols: list[SymbolBlock]
    parser: str


@dataclass(slots=True)
class SplitPlan:
    source_path: Path
    target_path: Path
    language: str
    parser: str
    selected_symbols: list[SymbolBlock]
    dependency_symbols: list[SymbolBlock]
    used_imports: list[ImportStatement]
    unused_selected_imports: list[ImportStatement]
    target_content: str
    source_content_after_move: str | None
    unresolved_identifiers: list[str]
    selection_misses: list[str]
    report: dict
    closure_analysis: dict = field(default_factory=dict)
    cross_target_imports: list[str] = field(default_factory=list)
    bucket: str | None = None
    replacement_text: str | None = None


@dataclass(slots=True)
class ClosureAnalysis:
    free_identifiers: list[str] = field(default_factory=list)
    import_dependencies: list[str] = field(default_factory=list)
    module_symbol_dependencies: list[str] = field(default_factory=list)
    closure_captures: list[str] = field(default_factory=list)
    declared_names: list[str] = field(default_factory=list)
    suggested_params: list[str] = field(default_factory=list)
    suggested_returns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RewriteOperation:
    start_line: int
    end_line: int
    replacement: str = ""
    description: str = ""


@dataclass(slots=True)
class SplitTargetSpec:
    path: Path | None
    symbols: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    include_local_deps: bool = False
    insert_source_import: bool = False
    keep_side_effect_imports: bool = True
    wrapper_kind: str | None = None
    wrapper_name: str | None = None
    wrapper_params: list[str] = field(default_factory=list)
    wrapper_returns: list[str] = field(default_factory=list)
    replace_with: str | None = None
    bucket: str | None = None
    name: str | None = None


@dataclass(slots=True)
class BatchPlanConfig:
    source_path: Path
    mode: str
    workspace_root: Path
    language: str | None
    verify_commands: list[str] = field(default_factory=list)
    verify_cwd: Path | None = None
    shared_target_dir: Path | None = None
    promote_shared_dependencies: bool = False
    promote_shared_types: bool = False
    rewrite_imports: bool = True
    auto_target_root: Path | None = None
    targets: list[SplitTargetSpec] = field(default_factory=list)


@dataclass(slots=True)
class BatchSplitPlan:
    config: BatchPlanConfig
    module: ModuleParseResult
    target_plans: list[SplitPlan]
    source_content_after_move: str | None
    dependency_graph: dict[str, list[str]]
    execution_order: list[str]
    verification_results: list[dict]
    report: dict


class SplitModuleError(RuntimeError):
    """Raised for user-facing split errors."""


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SplitModuleError(f"Could not read {path} as UTF-8: {exc}") from exc


def write_text(path: Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        raise SplitModuleError(f"Refusing to overwrite existing file without --force: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def maybe_backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup_path = path.with_suffix(path.suffix + ".bak")
    counter = 1
    while backup_path.exists():
        backup_path = path.with_suffix(path.suffix + f".bak{counter}")
        counter += 1
    shutil.copy2(path, backup_path)
    return backup_path


def detect_language(path: Path, explicit: str | None) -> str:
    if explicit:
        value = explicit.lower()
        if value in {"js", "jsx", "ts", "tsx", "javascript", "typescript", "js-like"}:
            return "js-like"
        if value in {"py", "python"}:
            return "python"
        raise SplitModuleError(f"Unsupported explicit language: {explicit}")
    suffix = path.suffix.lower()
    if suffix in JS_LIKE_EXTENSIONS:
        return "js-like"
    if suffix in PYTHON_EXTENSIONS:
        return "python"
    raise SplitModuleError(
        f"Could not infer language from extension '{suffix}'. Use --language."
    )


def offset_to_line(lines: Sequence[str], offset: int) -> int:
    running = 0
    for index, line in enumerate(lines, start=1):
        next_running = running + len(line)
        if offset < next_running:
            return index
        running = next_running
    return len(lines)


def slice_lines(lines: Sequence[str], start_line: int, end_line: int) -> str:
    return "".join(lines[start_line - 1 : end_line])


def line_start_offsets(lines: Sequence[str]) -> list[int]:
    offsets: list[int] = []
    running = 0
    for line in lines:
        offsets.append(running)
        running += len(line)
    return offsets


def parse_marker_regions(lines: Sequence[str]) -> list[SymbolBlock]:
    start_re = re.compile(rf"^\s*(?://|#)\s*{re.escape(DEFAULT_MARKER_START)}\s+(.+?)\s*$")
    end_re = re.compile(rf"^\s*(?://|#)\s*{re.escape(DEFAULT_MARKER_END)}\s+(.+?)\s*$")
    open_regions: dict[str, int] = {}
    regions: list[SymbolBlock] = []

    for line_no, raw_line in enumerate(lines, start=1):
        start_match = start_re.match(raw_line)
        if start_match:
            name = start_match.group(1).strip()
            if name in open_regions:
                raise SplitModuleError(
                    f"Duplicate open marker region '{name}' at line {line_no}."
                )
            open_regions[name] = line_no
            continue
        end_match = end_re.match(raw_line)
        if end_match:
            name = end_match.group(1).strip()
            start_line = open_regions.pop(name, None)
            if start_line is None:
                raise SplitModuleError(
                    f"Found end marker for unopened region '{name}' at line {line_no}."
                )
            regions.append(
                SymbolBlock(
                    name=name,
                    kind="region",
                    start_line=start_line,
                    end_line=line_no,
                    code=slice_lines(lines, start_line, line_no),
                    source="marker",
                )
            )

    if open_regions:
        dangling = ", ".join(f"{name}@{line}" for name, line in sorted(open_regions.items()))
        raise SplitModuleError(f"Unclosed split marker regions: {dangling}")

    return regions


def parse_python_import_names(node: ast.AST) -> tuple[list[str], str | None, bool]:
    if isinstance(node, ast.Import):
        names = [alias.asname or alias.name.split(".")[0] for alias in node.names]
        module = ", ".join(alias.name for alias in node.names)
        return names, module, False
    if isinstance(node, ast.ImportFrom):
        names = [alias.asname or alias.name for alias in node.names]
        module = "." * node.level + (node.module or "")
        return names, module, False
    return [], None, False


def parse_python_module(path: Path, text: str) -> ModuleParseResult:
    lines = text.splitlines(keepends=True)
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise SplitModuleError(f"Could not parse Python file {path}: {exc}") from exc

    imports: list[ImportStatement] = []
    symbols: list[SymbolBlock] = []

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            local_names, module, side_effect_only = parse_python_import_names(node)
            imports.append(
                ImportStatement(
                    text=slice_lines(lines, node.lineno, node.end_lineno or node.lineno),
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    local_names=local_names,
                    module=module,
                    side_effect_only=side_effect_only,
                )
            )
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "function" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else "class"
            symbols.append(
                SymbolBlock(
                    name=node.name,
                    kind=kind,
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    code=slice_lines(lines, node.lineno, node.end_lineno or node.lineno),
                )
            )
            continue

        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target_names: list[str] = []
            targets: Iterable[ast.AST]
            if isinstance(node, ast.Assign):
                targets = node.targets
            else:
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    target_names.append(target.id)
            for name in target_names:
                symbols.append(
                    SymbolBlock(
                        name=name,
                        kind="variable",
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        code=slice_lines(lines, node.lineno, node.end_lineno or node.lineno),
                    )
                )

    symbols.extend(parse_marker_regions(lines))
    symbols = dedupe_symbols(symbols)
    return ModuleParseResult(
        language="python",
        path=path,
        text=text,
        lines=lines,
        imports=imports,
        symbols=sorted(symbols, key=lambda item: (item.start_line, item.end_line, item.name)),
        parser="python-ast",
    )


def run_node_typescript_parser(path: Path) -> dict | None:
    node_script = r"""
const fs = require("fs");
const path = process.argv[1];
let ts;
try {
  ts = require("typescript");
} catch (error) {
  console.error("NO_TYPESCRIPT");
  process.exit(41);
}

const sourceText = fs.readFileSync(path, "utf8");
const lower = path.toLowerCase();
let kind = ts.ScriptKind.TS;
if (lower.endsWith(".tsx")) kind = ts.ScriptKind.TSX;
else if (lower.endsWith(".ts")) kind = ts.ScriptKind.TS;
else if (lower.endsWith(".jsx")) kind = ts.ScriptKind.JSX;
else if (lower.endsWith(".js") || lower.endsWith(".mjs") || lower.endsWith(".cjs")) kind = ts.ScriptKind.JS;

const sourceFile = ts.createSourceFile(path, sourceText, ts.ScriptTarget.Latest, true, kind);
const lineOf = (pos) => sourceFile.getLineAndCharacterOfPosition(pos).line + 1;

function statementText(node) {
  return sourceText.slice(node.getFullStart(), node.getEnd());
}

function extractImport(node) {
  const importClause = node.importClause;
  const localNames = [];
  let sideEffectOnly = true;
  if (importClause) {
    sideEffectOnly = false;
    if (importClause.name) {
      localNames.push(importClause.name.text);
    }
    if (importClause.namedBindings) {
      if (ts.isNamespaceImport(importClause.namedBindings)) {
        localNames.push(importClause.namedBindings.name.text);
      } else if (ts.isNamedImports(importClause.namedBindings)) {
        for (const element of importClause.namedBindings.elements) {
          localNames.push((element.name || element.propertyName).text);
        }
      }
    }
  }
  return {
    text: statementText(node),
    start_line: lineOf(node.getFullStart()),
    end_line: lineOf(node.getEnd()),
    local_names: localNames,
    module: node.moduleSpecifier && node.moduleSpecifier.text ? node.moduleSpecifier.text : null,
    side_effect_only: sideEffectOnly,
  };
}

function pushSymbol(symbols, node, name, kind, exported, defaultExport) {
  if (!name) return;
  symbols.push({
    name,
    kind,
    start_line: lineOf(node.getFullStart()),
    end_line: lineOf(node.getEnd()),
    code: statementText(node),
    exported,
    default_export: defaultExport,
    source: "symbol",
  });
}

const symbols = [];
const imports = [];

for (const statement of sourceFile.statements) {
  if (ts.isImportDeclaration(statement)) {
    imports.push(extractImport(statement));
    continue;
  }

  const modifiers = ts.canHaveModifiers(statement) ? ts.getModifiers(statement) : undefined;
  const modifierKinds = new Set((modifiers || []).map((modifier) => modifier.kind));
  const exported = modifierKinds.has(ts.SyntaxKind.ExportKeyword);
  const defaultExport = modifierKinds.has(ts.SyntaxKind.DefaultKeyword);

  if (ts.isFunctionDeclaration(statement)) {
    pushSymbol(symbols, statement, statement.name && statement.name.text, "function", exported, defaultExport);
    continue;
  }
  if (ts.isClassDeclaration(statement)) {
    pushSymbol(symbols, statement, statement.name && statement.name.text, "class", exported, defaultExport);
    continue;
  }
  if (ts.isInterfaceDeclaration(statement)) {
    pushSymbol(symbols, statement, statement.name.text, "interface", exported, defaultExport);
    continue;
  }
  if (ts.isTypeAliasDeclaration(statement)) {
    pushSymbol(symbols, statement, statement.name.text, "type", exported, defaultExport);
    continue;
  }
  if (ts.isEnumDeclaration(statement)) {
    pushSymbol(symbols, statement, statement.name.text, "enum", exported, defaultExport);
    continue;
  }
  if (ts.isVariableStatement(statement)) {
    for (const declaration of statement.declarationList.declarations) {
      if (ts.isIdentifier(declaration.name)) {
        pushSymbol(symbols, statement, declaration.name.text, "variable", exported, defaultExport);
      }
    }
    continue;
  }
}

console.log(JSON.stringify({ imports, symbols }));
"""

    try:
        completed = subprocess.run(
            ["node", "-e", node_script, str(path)],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(path.parent),
        )
    except FileNotFoundError:
        return None

    if completed.returncode == 41 and "NO_TYPESCRIPT" in completed.stderr:
        return None
    if completed.returncode != 0:
        raise SplitModuleError(
            f"TypeScript parser subprocess failed for {path}.\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SplitModuleError(
            f"TypeScript parser returned invalid JSON for {path}: {exc}"
        ) from exc


def dedupe_symbols(symbols: Sequence[SymbolBlock]) -> list[SymbolBlock]:
    seen: set[tuple[str, int, int, str]] = set()
    deduped: list[SymbolBlock] = []
    for symbol in symbols:
        key = (symbol.name, symbol.start_line, symbol.end_line, symbol.kind)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(symbol)
    return deduped


def parse_js_import_statement_fallback(statement_text: str) -> tuple[list[str], str | None, bool]:
    stripped = statement_text.strip()
    if re.fullmatch(r'import\s+["\'][^"\']+["\'];?', stripped):
        module_match = re.search(r'["\']([^"\']+)["\']', stripped)
        return [], module_match.group(1) if module_match else None, True

    module_match = re.search(r'\bfrom\s+["\']([^"\']+)["\']', stripped)
    module = module_match.group(1) if module_match else None
    local_names: list[str] = []

    named_match = re.search(r'import\s+(.*?)\s+from\s+["\']', stripped, flags=re.S)
    if not named_match:
        return local_names, module, False
    header = named_match.group(1).strip()
    if header.startswith("{"):
        members = header.strip("{} \n\t")
        for member in members.split(","):
            part = member.strip()
            if not part:
                continue
            if " as " in part:
                _, alias = part.split(" as ", 1)
                local_names.append(alias.strip())
            else:
                local_names.append(part)
    elif header.startswith("* as "):
        local_names.append(header[5:].strip())
    elif "," in header:
        default_name, remainder = header.split(",", 1)
        if default_name.strip():
            local_names.append(default_name.strip())
        remainder = remainder.strip()
        if remainder.startswith("{"):
            members = remainder.strip("{} \n\t")
            for member in members.split(","):
                part = member.strip()
                if not part:
                    continue
                if " as " in part:
                    _, alias = part.split(" as ", 1)
                    local_names.append(alias.strip())
                else:
                    local_names.append(part)
        elif remainder.startswith("* as "):
            local_names.append(remainder[5:].strip())
    else:
        local_names.append(header)
    return local_names, module, False


def strip_js_comments_and_strings(code: str) -> str:
    result: list[str] = []
    i = 0
    length = len(code)
    in_single = False
    in_double = False
    in_template = False
    template_brace_depth = 0
    in_line_comment = False
    in_block_comment = False

    while i < length:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < length else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                result.append("\n")
            else:
                result.append(" ")
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                result.extend("  ")
                in_block_comment = False
                i += 2
            else:
                result.append("\n" if ch == "\n" else " ")
                i += 1
            continue

        if in_single:
            if ch == "\\":
                result.extend("  ")
                i += 2
                continue
            if ch == "'":
                in_single = False
            result.append("\n" if ch == "\n" else " ")
            i += 1
            continue

        if in_double:
            if ch == "\\":
                result.extend("  ")
                i += 2
                continue
            if ch == '"':
                in_double = False
            result.append("\n" if ch == "\n" else " ")
            i += 1
            continue

        if in_template:
            if ch == "\\":
                result.extend("  ")
                i += 2
                continue
            if ch == "`" and template_brace_depth == 0:
                in_template = False
                result.append(" ")
                i += 1
                continue
            if ch == "$" and nxt == "{":
                template_brace_depth += 1
                result.extend("  ")
                i += 2
                continue
            if ch == "}" and template_brace_depth > 0:
                template_brace_depth -= 1
                result.append(" ")
                i += 1
                continue
            result.append("\n" if ch == "\n" else " ")
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            result.extend("  ")
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            result.extend("  ")
            i += 2
            continue
        if ch == "'":
            in_single = True
            result.append(" ")
            i += 1
            continue
        if ch == '"':
            in_double = True
            result.append(" ")
            i += 1
            continue
        if ch == "`":
            in_template = True
            template_brace_depth = 0
            result.append(" ")
            i += 1
            continue

        result.append(ch)
        i += 1

    return "".join(result)


def js_identifier_tokens(code: str) -> set[str]:
    stripped = strip_js_comments_and_strings(code)
    tokens = set()
    for match in re.finditer(r"(?<![\.\w$])([A-Za-z_$][A-Za-z0-9_$]*)\b", stripped):
        token = match.group(1)
        if token in JS_KEYWORDS or token in COMMON_JS_GLOBALS:
            continue
        tokens.add(token)
    return tokens


def strip_python_strings_and_comments(code: str) -> str:
    try:
        import io
        import tokenize
    except ImportError:
        return code

    output: list[str] = []
    previous_end = (1, 0)
    readline = io.StringIO(code).readline
    for token_type, token_string, start, end, _ in tokenize.generate_tokens(readline):
        start_line, start_col = start
        end_line, end_col = end
        if start_line > previous_end[0]:
            output.append("\n" * (start_line - previous_end[0]))
            output.append(" " * start_col)
        else:
            output.append(" " * max(start_col - previous_end[1], 0))
        if token_type in {tokenize.STRING, tokenize.COMMENT}:
            replacement = "\n" * token_string.count("\n")
            if not replacement:
                replacement = " " * len(token_string)
            output.append(replacement)
        else:
            output.append(token_string)
        previous_end = (end_line, end_col)
    return "".join(output)


def python_identifier_tokens(code: str) -> set[str]:
    stripped = strip_python_strings_and_comments(code)
    tokens = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", stripped))
    return {token for token in tokens if token not in COMMON_PYTHON_GLOBALS}


def python_free_identifiers(code: str) -> set[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return python_identifier_tokens(code)

    declared: set[str] = set()
    used: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                declared.add(node.id)
            elif isinstance(node.ctx, ast.Load):
                used.add(node.id)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            declared.add(node.name)
            self._declare_args(node.args)
            for decorator in node.decorator_list:
                self.visit(decorator)
            if node.returns is not None:
                self.visit(node.returns)
            for child in node.body:
                self.visit(child)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            declared.add(node.name)
            for decorator in node.decorator_list:
                self.visit(decorator)
            for base in node.bases:
                self.visit(base)
            for keyword_node in node.keywords:
                self.visit(keyword_node)
            for child in node.body:
                self.visit(child)

        def visit_alias(self, node: ast.alias) -> None:
            declared.add(node.asname or node.name.split(".")[0])

        def visit_With(self, node: ast.With) -> None:
            for item in node.items:
                self.visit(item.context_expr)
                if item.optional_vars is not None:
                    self.visit(item.optional_vars)
            for child in node.body:
                self.visit(child)

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
            self.visit_With(node)

        def visit_comprehension(self, node: ast.comprehension) -> None:
            self.visit(node.target)
            self.visit(node.iter)
            for if_node in node.ifs:
                self.visit(if_node)

        def _declare_args(self, args: ast.arguments) -> None:
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                declared.add(arg.arg)
            if args.vararg:
                declared.add(args.vararg.arg)
            if args.kwarg:
                declared.add(args.kwarg.arg)
            for default in [*args.defaults, *args.kw_defaults]:
                if default is not None:
                    self.visit(default)

    Visitor().visit(tree)
    return {
        name
        for name in used - declared
        if name not in COMMON_PYTHON_GLOBALS and not keyword.iskeyword(name)
    }


def js_free_identifiers(code: str, filename_hint: str = "snippet.tsx") -> set[str]:
    node_script = r"""
const filename = process.argv[1];
let ts;
try {
  ts = require("typescript");
} catch (error) {
  console.error("NO_TYPESCRIPT");
  process.exit(41);
}
const sourceText = require("fs").readFileSync(0, "utf8");

const lower = filename.toLowerCase();
let kind = ts.ScriptKind.TS;
if (lower.endsWith(".tsx")) kind = ts.ScriptKind.TSX;
else if (lower.endsWith(".ts")) kind = ts.ScriptKind.TS;
else if (lower.endsWith(".jsx")) kind = ts.ScriptKind.JSX;
else if (lower.endsWith(".js") || lower.endsWith(".mjs") || lower.endsWith(".cjs")) kind = ts.ScriptKind.JS;

const sourceFile = ts.createSourceFile(filename, sourceText, ts.ScriptTarget.Latest, true, kind);
const declared = new Set();
const used = new Set();

function declareBindingName(name) {
  if (!name) return;
  if (ts.isIdentifier(name)) {
    declared.add(name.text);
    return;
  }
  if (ts.isObjectBindingPattern(name) || ts.isArrayBindingPattern(name)) {
    for (const element of name.elements) {
      if (ts.isBindingElement(element)) {
        declareBindingName(element.name);
      }
    }
  }
}

function isDeclarationIdentifier(node, parent) {
  if (!parent) return false;
  if (
    (ts.isFunctionDeclaration(parent) ||
      ts.isClassDeclaration(parent) ||
      ts.isInterfaceDeclaration(parent) ||
      ts.isTypeAliasDeclaration(parent) ||
      ts.isEnumDeclaration(parent) ||
      ts.isEnumMember(parent) ||
      ts.isTypeParameterDeclaration(parent) ||
      ts.isParameter(parent) ||
      ts.isImportClause(parent) ||
      ts.isImportSpecifier(parent) ||
      ts.isNamespaceImport(parent) ||
      ts.isImportEqualsDeclaration(parent) ||
      ts.isBindingElement(parent) ||
      ts.isVariableDeclaration(parent)) &&
    parent.name === node
  ) {
    return true;
  }
  if (ts.isPropertyAssignment(parent) && parent.name === node && !parent.shorthandInitializer) {
    return true;
  }
  if (ts.isJsxAttribute(parent) && parent.name === node) {
    return true;
  }
  if (ts.isBindingElement(parent) && parent.propertyName === node) {
    return true;
  }
  if (
    (ts.isPropertySignature(parent) ||
      ts.isPropertyDeclaration(parent) ||
      ts.isMethodDeclaration(parent) ||
      ts.isMethodSignature(parent) ||
      ts.isGetAccessorDeclaration(parent) ||
      ts.isSetAccessorDeclaration(parent)) &&
    parent.name === node
  ) {
    return true;
  }
  if (ts.isPropertyAccessExpression(parent) && parent.name === node) {
    return true;
  }
  if (ts.isQualifiedName(parent) && parent.right === node) {
    return true;
  }
  if (
    (ts.isJsxOpeningElement(parent) ||
      ts.isJsxSelfClosingElement(parent) ||
      ts.isJsxClosingElement(parent)) &&
    parent.tagName === node &&
    /^[a-z]/.test(node.text)
  ) {
    return true;
  }
  if (ts.isLabeledStatement(parent) && parent.label === node) {
    return true;
  }
  return false;
}

function visit(node) {
  if (ts.isVariableDeclaration(node)) {
    declareBindingName(node.name);
  } else if (ts.isParameter(node)) {
    declareBindingName(node.name);
  } else if (
    ts.isFunctionDeclaration(node) ||
    ts.isClassDeclaration(node) ||
    ts.isInterfaceDeclaration(node) ||
    ts.isTypeAliasDeclaration(node) ||
    ts.isEnumDeclaration(node)
  ) {
    if (node.name) {
      declared.add(node.name.text);
    }
  } else if (ts.isImportClause(node)) {
    if (node.name) {
      declared.add(node.name.text);
    }
  } else if (ts.isImportSpecifier(node)) {
    declared.add(node.name.text);
  } else if (ts.isNamespaceImport(node)) {
    declared.add(node.name.text);
  } else if (ts.isImportEqualsDeclaration(node)) {
    declared.add(node.name.text);
  } else if (ts.isBindingElement(node)) {
    declareBindingName(node.name);
  } else if (ts.isIdentifier(node)) {
    if (!isDeclarationIdentifier(node, node.parent)) {
      used.add(node.text);
    }
  }

  ts.forEachChild(node, visit);
}

visit(sourceFile);
console.log(JSON.stringify(Array.from(used).filter((name) => !declared.has(name))));
"""

    try:
        completed = subprocess.run(
            ["node", "-e", node_script, filename_hint],
            capture_output=True,
            text=True,
            check=False,
            input=code,
        )
    except FileNotFoundError:
        return js_identifier_tokens(code)

    if completed.returncode == 41 and "NO_TYPESCRIPT" in completed.stderr:
        return js_identifier_tokens(code)
    if completed.returncode != 0:
        return js_identifier_tokens(code)

    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return js_identifier_tokens(code)

    return {
        value
        for value in values
        if isinstance(value, str) and value not in COMMON_JS_GLOBALS and value not in JS_KEYWORDS
    }


def fallback_parse_js_module(path: Path, text: str) -> ModuleParseResult:
    lines = text.splitlines(keepends=True)
    offsets = line_start_offsets(lines)
    text_length = len(text)

    imports: list[ImportStatement] = []
    symbols: list[SymbolBlock] = []

    line_no = 1
    while line_no <= len(lines):
        line = lines[line_no - 1]
        stripped = line.strip()
        if not stripped:
            line_no += 1
            continue

        if stripped.startswith("import "):
            start_line = line_no
            statement = line
            while not statement.rstrip().endswith(";") and line_no < len(lines):
                line_no += 1
                statement += lines[line_no - 1]
            local_names, module, side_effect_only = parse_js_import_statement_fallback(statement)
            imports.append(
                ImportStatement(
                    text=statement,
                    start_line=start_line,
                    end_line=line_no,
                    local_names=local_names,
                    module=module,
                    side_effect_only=side_effect_only,
                )
            )
            line_no += 1
            continue

        decl_match = re.match(
            r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
            r"(function|class|interface|type|enum|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
            line,
        )
        if decl_match:
            kind = decl_match.group(1)
            name = decl_match.group(2)
            start_line = line_no
            start_offset = offsets[start_line - 1]
            end_offset = find_js_statement_end(text, start_offset)
            end_line = offset_to_line(lines, end_offset - 1 if end_offset > 0 else 0)
            code = text[start_offset:end_offset]
            exported = bool(re.match(r"^\s*export\b", line))
            default_export = bool(re.match(r"^\s*export\s+default\b", line))
            symbols.append(
                SymbolBlock(
                    name=name,
                    kind="variable" if kind in {"const", "let", "var"} else kind,
                    start_line=start_line,
                    end_line=end_line,
                    code=code,
                    exported=exported,
                    default_export=default_export,
                )
            )
            line_no = end_line + 1
            continue
        line_no += 1

    symbols.extend(parse_marker_regions(lines))
    symbols = dedupe_symbols(symbols)
    return ModuleParseResult(
        language="js-like",
        path=path,
        text=text,
        lines=lines,
        imports=imports,
        symbols=sorted(symbols, key=lambda item: (item.start_line, item.end_line, item.name)),
        parser="python-fallback-scanner",
    )


def find_js_statement_end(text: str, start_offset: int) -> int:
    i = start_offset
    length = len(text)
    brace_depth = 0
    paren_depth = 0
    bracket_depth = 0
    seen_body = False
    in_single = False
    in_double = False
    in_template = False
    template_brace_depth = 0
    in_line_comment = False
    in_block_comment = False

    while i < length:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < length else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue

        if in_single:
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue

        if in_double:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue

        if in_template:
            if ch == "\\":
                i += 2
                continue
            if ch == "$" and nxt == "{":
                template_brace_depth += 1
                i += 2
                continue
            if ch == "}" and template_brace_depth > 0:
                template_brace_depth -= 1
                i += 1
                continue
            if ch == "`" and template_brace_depth == 0:
                in_template = False
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        if ch == "`":
            in_template = True
            template_brace_depth = 0
            i += 1
            continue

        if ch == "{":
            brace_depth += 1
            seen_body = True
            i += 1
            continue
        if ch == "}":
            brace_depth = max(0, brace_depth - 1)
            i += 1
            if seen_body and brace_depth == 0 and paren_depth == 0 and bracket_depth == 0:
                while i < length and text[i] in " \t":
                    i += 1
                if i < length and text[i] == ";":
                    i += 1
                return i
            continue
        if ch == "(":
            paren_depth += 1
            i += 1
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            i += 1
            continue
        if ch == "[":
            bracket_depth += 1
            i += 1
            continue
        if ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
            i += 1
            continue
        if ch == ";" and brace_depth == 0 and paren_depth == 0 and bracket_depth == 0:
            return i + 1
        i += 1

    return length


def parse_js_module(path: Path, text: str) -> ModuleParseResult:
    lines = text.splitlines(keepends=True)
    parsed = run_node_typescript_parser(path)
    if parsed is None:
        return fallback_parse_js_module(path, text)

    imports = [
        ImportStatement(
            text=item["text"],
            start_line=item["start_line"],
            end_line=item["end_line"],
            local_names=item.get("local_names", []),
            module=item.get("module"),
            side_effect_only=item.get("side_effect_only", False),
        )
        for item in parsed.get("imports", [])
    ]
    symbols = [
        SymbolBlock(
            name=item["name"],
            kind=item["kind"],
            start_line=item["start_line"],
            end_line=item["end_line"],
            code=item["code"],
            exported=item.get("exported", False),
            default_export=item.get("default_export", False),
            source=item.get("source", "symbol"),
        )
        for item in parsed.get("symbols", [])
        if item.get("name")
    ]
    symbols.extend(parse_marker_regions(lines))
    symbols = dedupe_symbols(symbols)
    return ModuleParseResult(
        language="js-like",
        path=path,
        text=text,
        lines=lines,
        imports=imports,
        symbols=sorted(symbols, key=lambda item: (item.start_line, item.end_line, item.name)),
        parser="node-typescript",
    )


def parse_module(path: Path, language: str) -> ModuleParseResult:
    text = read_text(path)
    if language == "python":
        return parse_python_module(path, text)
    if language == "js-like":
        return parse_js_module(path, text)
    raise SplitModuleError(f"Unsupported language: {language}")


def parse_csvish(values: Sequence[str] | None) -> list[str]:
    if not values:
        return []
    items: list[str] = []
    for value in values:
        for part in value.split(","):
            cleaned = part.strip()
            if cleaned:
                items.append(cleaned)
    return items


def coerce_string_list(value: object, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in parse_csvish([value]) if item]
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise SplitModuleError(f"Expected string items in '{field_name}'.")
            output.extend(parse_csvish([item]))
        return output
    raise SplitModuleError(f"Expected string or list of strings for '{field_name}'.")


def read_json(path: Path) -> object:
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise SplitModuleError(f"Invalid JSON in {path}: {exc}") from exc


def resolve_maybe_relative_path(path_value: str, base_dir: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def default_extension_for_language(language: str, source_path: Path) -> str:
    if language == "python":
        return ".py"
    return source_path.suffix or ".ts"


def symbol_output_basename(symbol_names: Sequence[str], fallback: str) -> str:
    if symbol_names:
        if len(symbol_names) == 1:
            return slugify_checkpoint_label(symbol_names[0])
        return slugify_checkpoint_label("-".join(symbol_names[:4]))
    return slugify_checkpoint_label(fallback)


def bucketize_symbol(symbol: SymbolBlock) -> str:
    name = symbol.name.lower()
    if symbol.kind in {"interface", "type", "enum"}:
        return "shared"
    if symbol.name.endswith(("Props", "State", "Options", "Config", "Context", "Contract")):
        return "shared"
    if name.startswith(("parse", "format", "normalize", "serialize", "truncate", "compact", "slugify", "extract", "summarize", "classify", "infer", "score", "select")):
        return "helpers"
    if name.startswith(("run", "call", "send", "stream", "abort", "stop", "handle")):
        return "runtime"
    if "workspace" in name or "validation" in name or "checkpoint" in name or "plan" in name or "tool" in name:
        return "workspace-agent"
    if symbol.name[:1].isupper():
        return "chat-shell"
    return "chat-helpers"


def apply_auto_target_path(
    spec: SplitTargetSpec,
    source_path: Path,
    language: str,
    auto_target_root: Path | None,
) -> SplitTargetSpec:
    if spec.path:
        return spec
    if auto_target_root is None:
        raise SplitModuleError("A batch target omitted 'path' without --auto-target-root or plan.autoTargetRoot.")
    bucket = spec.bucket or "chat-helpers"
    base_name = symbol_output_basename(spec.symbols or spec.regions or spec.queries, "split-target")
    extension = default_extension_for_language(language, source_path)
    spec.path = (auto_target_root / bucket / f"{base_name}{extension}").resolve()
    return spec


def normalize_name(value: str) -> str:
    return re.sub(r"[\W_]+", "", value).lower()


def score_query_against_symbol(query: str, symbol: SymbolBlock) -> float:
    normalized_query = normalize_name(query)
    normalized_name = normalize_name(symbol.name)
    if not normalized_query:
        return 0.0
    if normalized_query == normalized_name:
        return 1.0
    if normalized_query in normalized_name:
        return 0.82
    if normalized_name in normalized_query:
        return 0.78
    if query.lower() == symbol.kind.lower():
        return 0.75
    ratio = difflib.SequenceMatcher(None, normalized_query, normalized_name).ratio()
    if ratio >= 0.72:
        return ratio * 0.7
    tokens = {normalize_name(token) for token in re.split(r"[\s/_-]+", query) if token.strip()}
    if tokens and all(token and token in normalized_name for token in tokens):
        return 0.68
    return 0.0


def select_symbols(
    symbols: Sequence[SymbolBlock],
    exact_names: Sequence[str],
    intent_queries: Sequence[str],
) -> tuple[list[SymbolBlock], list[str]]:
    by_name: dict[str, list[SymbolBlock]] = {}
    for symbol in symbols:
        by_name.setdefault(symbol.name.lower(), []).append(symbol)

    selected: list[SymbolBlock] = []
    misses: list[str] = []
    seen_keys: set[tuple[str, int, int]] = set()

    def add_symbol(symbol: SymbolBlock, matched_by: str) -> None:
        key = (symbol.name, symbol.start_line, symbol.end_line)
        if key in seen_keys:
            return
        seen_keys.add(key)
        symbol.matched_by.append(matched_by)
        selected.append(symbol)

    for name in exact_names:
        matches = by_name.get(name.lower(), [])
        if not matches:
            misses.append(name)
            continue
        for match in matches:
            add_symbol(match, f"symbol:{name}")

    for query in intent_queries:
        lowered = query.lower()
        if lowered.startswith("kind:"):
            wanted_kind = lowered.split(":", 1)[1].strip()
            kind_matches = [symbol for symbol in symbols if symbol.kind.lower() == wanted_kind]
            if not kind_matches:
                misses.append(query)
                continue
            for match in kind_matches:
                add_symbol(match, f"query:{query}")
            continue

        if lowered.startswith("name:"):
            wanted_name = lowered.split(":", 1)[1].strip()
            matches = by_name.get(wanted_name.lower(), [])
            if not matches:
                misses.append(query)
                continue
            for match in matches:
                add_symbol(match, f"query:{query}")
            continue

        scored: list[tuple[float, SymbolBlock]] = []
        for symbol in symbols:
            score = score_query_against_symbol(query, symbol)
            if score > 0:
                scored.append((score, symbol))
        scored.sort(key=lambda item: (-item[0], item[1].start_line, item[1].name))
        if not scored:
            misses.append(query)
            continue
        top_score = scored[0][0]
        winners = [symbol for score, symbol in scored if score >= max(0.68, top_score - 0.12)]
        for winner in winners:
            add_symbol(winner, f"query:{query}")

    return sorted(selected, key=lambda item: (item.start_line, item.end_line, item.name)), misses


def tokens_for_language(code: str, language: str, filename_hint: str | None = None) -> set[str]:
    if language == "python":
        return python_free_identifiers(code)
    return js_free_identifiers(code, filename_hint or "snippet.tsx")


def declared_names_from_snippet(code: str, language: str, filename_hint: str) -> list[str]:
    if language == "python":
        try:
            parsed = parse_python_module(Path(filename_hint), code)
        except SplitModuleError:
            return []
    else:
        try:
            parsed = parse_js_module(Path(filename_hint), code)
        except SplitModuleError:
            return []
    return [symbol.name for symbol in parsed.symbols if symbol.source == "symbol"]


def analyze_closure_context(
    module: ModuleParseResult,
    selected_symbols: Sequence[SymbolBlock],
    dependency_symbols: Sequence[SymbolBlock],
    used_imports: Sequence[ImportStatement],
) -> ClosureAnalysis:
    code = "\n".join(symbol.code for symbol in [*selected_symbols, *dependency_symbols])
    free_identifiers = sorted(tokens_for_language(code, module.language, module.path.name))
    import_names = {name for import_stmt in used_imports for name in import_stmt.local_names}
    selected_names = {symbol.name for symbol in [*selected_symbols, *dependency_symbols]}
    module_symbol_names = {symbol.name for symbol in module.symbols}
    module_symbol_deps = sorted(
        name
        for name in free_identifiers
        if name in module_symbol_names and name not in selected_names
    )
    import_deps = sorted(name for name in free_identifiers if name in import_names)
    closure_captures = sorted(
        name
        for name in free_identifiers
        if name not in import_names and name not in selected_names and name not in module_symbol_names
    )
    declared_names = declared_names_from_snippet(code, module.language, module.path.name)
    suggested_returns = [
        name
        for name in declared_names
        if name not in selected_names and not name[:1].isupper()
    ]
    notes: list[str] = []
    if closure_captures:
        notes.append("Selected content captures identifiers that are not imports or top-level symbols.")
    if module_symbol_deps:
        notes.append("Selected content depends on other top-level symbols that may need shared promotion or cross-target imports.")
    return ClosureAnalysis(
        free_identifiers=free_identifiers,
        import_dependencies=import_deps,
        module_symbol_dependencies=module_symbol_deps,
        closure_captures=closure_captures,
        declared_names=declared_names,
        suggested_params=sorted(dict.fromkeys(closure_captures)),
        suggested_returns=sorted(dict.fromkeys(suggested_returns)),
        notes=notes,
    )


def render_js_identifier_object(name: str) -> str:
    return name


def indent_block(text: str, prefix: str = "  ") -> str:
    lines = text.splitlines()
    return "\n".join((prefix + line) if line else "" for line in lines)


def dedupe_import_block(lines: list[str], language: str) -> list[str]:
    import_pattern = (
        re.compile(r"^\s*(import\b|from\b)")
        if language == "python"
        else re.compile(r"^\s*import\b")
    )
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        normalized = line.strip()
        if import_pattern.match(line):
            if normalized in seen:
                continue
            seen.add(normalized)
        output.append(line)
    return output


def prepend_imports_to_content(content: str, import_statements: Sequence[str], language: str) -> str:
    if not import_statements:
        return content
    current_lines = content.splitlines()
    import_lines = [statement.rstrip() for statement in import_statements if statement.strip()]
    merged_lines = dedupe_import_block(import_lines + [""] + current_lines, language)
    return normalize_blank_runs("\n".join(merged_lines))


def build_cross_target_import_statement(
    language: str,
    from_path: Path,
    to_path: Path,
    names: Sequence[str],
) -> str:
    if not names:
        return ""
    if language == "python":
        module_ref = compute_module_reference(from_path, to_path, language)
        return f"from {module_ref} import {', '.join(names)}"
    module_ref = compute_module_reference(from_path, to_path, language)
    return f'import {{ {", ".join(names)} }} from "{module_ref}";'


def build_wrapper_content(
    module: ModuleParseResult,
    spec: SplitTargetSpec,
    selected_symbols: Sequence[SymbolBlock],
    dependency_symbols: Sequence[SymbolBlock],
    imports: Sequence[ImportStatement],
    closure: ClosureAnalysis,
) -> str:
    wrapper_kind = (spec.wrapper_kind or "").lower()
    if not wrapper_kind:
        return build_target_content(
            module=module,
            selected_symbols=selected_symbols,
            dependency_symbols=dependency_symbols,
            used_imports=imports,
            add_named_exports=spec.insert_source_import and module.language == "js-like",
        )

    import_lines = [import_stmt.text.rstrip() for import_stmt in imports]
    helper_blocks = [symbol.code.rstrip() for symbol in dependency_symbols]
    body_blocks = [symbol.code.rstrip() for symbol in selected_symbols]
    body = "\n\n".join(block for block in body_blocks if block.strip())
    if not body:
        return build_target_content(
            module=module,
            selected_symbols=selected_symbols,
            dependency_symbols=dependency_symbols,
            used_imports=imports,
            add_named_exports=spec.insert_source_import and module.language == "js-like",
        )

    wrapper_name = spec.wrapper_name or (
        ("use" + selected_symbols[0].name[:1].upper() + selected_symbols[0].name[1:])
        if wrapper_kind == "hook" and selected_symbols
        else (selected_symbols[0].name if selected_symbols else "extractedModule")
    )
    params = spec.wrapper_params or closure.suggested_params
    returns = spec.wrapper_returns or closure.suggested_returns

    if module.language == "python":
        signature = ", ".join(params)
        return_lines = ""
        if returns:
            return_lines = f"\n    return {{ {', '.join(f'\"{name}\": {name}' for name in returns)} }}"
        wrapper_block = f"def {wrapper_name}({signature}):\n{indent_block(body, '    ')}{return_lines}\n"
    else:
        if params:
            param_block = "{ " + ", ".join(render_js_identifier_object(name) for name in params) + " }"
        else:
            param_block = ""
        return_lines = ""
        if returns:
            return_lines = f"\n  return {{ {', '.join(returns)} }};"
        export_prefix = "export "
        wrapper_keyword = "function" if wrapper_kind in {"hook", "function"} else "const"
        if wrapper_keyword == "function":
            wrapper_block = (
                f"{export_prefix}function {wrapper_name}({param_block}) {{\n"
                f"{indent_block(body, '  ')}{return_lines}\n"
                f"}}\n"
            )
        else:
            wrapper_block = (
                f"{export_prefix}const {wrapper_name} = ({param_block}) => {{\n"
                f"{indent_block(body, '  ')}{return_lines}\n"
                f"}};\n"
            )

    chunks: list[str] = []
    if import_lines:
        chunks.append("\n".join(import_lines))
    if helper_blocks:
        chunks.append("\n\n".join(helper_blocks))
    chunks.append(wrapper_block.rstrip())
    return normalize_blank_runs("\n\n".join(chunk for chunk in chunks if chunk.strip()))


def maybe_build_region_replacement(
    spec: SplitTargetSpec,
    selected_symbols: Sequence[SymbolBlock],
    closure: ClosureAnalysis,
) -> str | None:
    if spec.replace_with is not None:
        return spec.replace_with
    if (spec.wrapper_kind or "").lower() != "hook":
        return None
    if not selected_symbols:
        return None
    if not any(symbol.source == "marker" for symbol in selected_symbols):
        return None
    wrapper_name = spec.wrapper_name or f"use{selected_symbols[0].name[:1].upper()}{selected_symbols[0].name[1:]}"
    params = spec.wrapper_params or closure.suggested_params
    returns = spec.wrapper_returns or closure.suggested_returns
    args = ", ".join(params)
    if returns:
        return f"const {{ {', '.join(returns)} }} = {wrapper_name}({{{args}}});"
    if args:
        return f"{wrapper_name}({{{args}}});"
    return f"{wrapper_name}();"


def resolve_local_dependencies(
    language: str,
    selected_symbols: Sequence[SymbolBlock],
    all_symbols: Sequence[SymbolBlock],
    import_names: set[str],
    include_local_deps: bool,
    filename_hint: str | None = None,
) -> list[SymbolBlock]:
    if not include_local_deps:
        return []

    selected_keys = {(symbol.name, symbol.start_line, symbol.end_line) for symbol in selected_symbols}
    by_name: dict[str, list[SymbolBlock]] = {}
    for symbol in all_symbols:
        key = (symbol.name, symbol.start_line, symbol.end_line)
        if key in selected_keys:
            continue
        by_name.setdefault(symbol.name, []).append(symbol)

    added: list[SymbolBlock] = []
    changed = True
    current = list(selected_symbols)
    while changed:
        changed = False
        combined_code = "\n".join(symbol.code for symbol in current)
        used_names = tokens_for_language(combined_code, language, filename_hint)
        for imported_name in import_names:
            used_names.discard(imported_name)
        for symbol_name in sorted(used_names):
            for candidate in by_name.get(symbol_name, []):
                key = (candidate.name, candidate.start_line, candidate.end_line)
                if key in selected_keys:
                    continue
                selected_keys.add(key)
                candidate.matched_by.append("local-dependency")
                added.append(candidate)
                current.append(candidate)
                changed = True
    return sorted(added, key=lambda item: (item.start_line, item.end_line, item.name))


def choose_used_imports(
    module: ModuleParseResult,
    selected_symbols: Sequence[SymbolBlock],
    dependency_symbols: Sequence[SymbolBlock],
    keep_side_effect_imports: bool,
) -> tuple[list[ImportStatement], list[ImportStatement]]:
    all_selected_code = "\n".join(
        symbol.code for symbol in [*selected_symbols, *dependency_symbols]
    )
    used_names = tokens_for_language(all_selected_code, module.language, module.path.name)

    used_imports: list[ImportStatement] = []
    unused_imports: list[ImportStatement] = []
    for import_stmt in module.imports:
        if import_stmt.side_effect_only:
            if keep_side_effect_imports:
                used_imports.append(import_stmt)
            else:
                unused_imports.append(import_stmt)
            continue
        if any(name in used_names for name in import_stmt.local_names):
            used_imports.append(import_stmt)
        else:
            unused_imports.append(import_stmt)
    return used_imports, unused_imports


def unresolved_identifiers(
    module: ModuleParseResult,
    selected_symbols: Sequence[SymbolBlock],
    dependency_symbols: Sequence[SymbolBlock],
    used_imports: Sequence[ImportStatement],
) -> list[str]:
    code = "\n".join(symbol.code for symbol in [*selected_symbols, *dependency_symbols])
    tokens = tokens_for_language(code, module.language, module.path.name)
    known_names = {symbol.name for symbol in selected_symbols}
    known_names.update(symbol.name for symbol in dependency_symbols)
    for import_stmt in used_imports:
        known_names.update(import_stmt.local_names)
    if module.language == "python":
        known_names.update(COMMON_PYTHON_GLOBALS)
    else:
        known_names.update(COMMON_JS_GLOBALS)
        known_names.update(JS_KEYWORDS)
    unresolved = sorted(token for token in tokens if token not in known_names)
    return unresolved


def normalize_blank_runs(text: str) -> str:
    lines = text.splitlines()
    normalized: list[str] = []
    blank_run = 0
    for line in lines:
        if line.strip():
            blank_run = 0
            normalized.append(line.rstrip())
            continue
        blank_run += 1
        if blank_run <= 2:
            normalized.append("")
    return "\n".join(normalized).rstrip() + "\n"


def ensure_js_named_export(code: str) -> str:
    stripped = code.lstrip()
    if stripped.startswith("export "):
        return code
    for keyword in ("async function", "function", "class", "interface", "type", "enum", "const", "let", "var"):
        needle = re.compile(rf"^(\s*){re.escape(keyword)}\b", flags=re.M)
        if needle.search(code):
            return needle.sub(r"\1export " + keyword, code, count=1)
    return "export " + code


def build_target_content(
    module: ModuleParseResult,
    selected_symbols: Sequence[SymbolBlock],
    dependency_symbols: Sequence[SymbolBlock],
    used_imports: Sequence[ImportStatement],
    add_named_exports: bool,
) -> str:
    chunks: list[str] = []
    if used_imports:
        chunks.append("".join(import_stmt.text for import_stmt in used_imports).rstrip() + "\n")

    blocks: list[str] = []
    ordered_symbols = sorted(
        [*selected_symbols, *dependency_symbols],
        key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.name),
    )
    for symbol in ordered_symbols:
        code = symbol.code
        if add_named_exports and module.language == "js-like":
            code = ensure_js_named_export(code)
        blocks.append(code.rstrip() + "\n")

    if blocks:
        chunks.append("\n".join(blocks).rstrip() + "\n")
    return normalize_blank_runs("\n".join(chunk.rstrip() for chunk in chunks if chunk.strip()))


def compute_module_reference(source_path: Path, target_path: Path, language: str) -> str:
    if language == "python":
        relative = target_path.with_suffix("")
        source_parent = source_path.parent
        try:
            rel = relative.relative_to(source_parent)
            parts = list(rel.parts)
            return ".".join(parts)
        except ValueError:
            rel_path = os.path.relpath(relative, source_parent)
            module = rel_path.replace(os.sep, ".")
            module = re.sub(r"\.+", ".", module)
            module = module.lstrip(".")
            return module

    relative = os.path.relpath(target_path, source_path.parent)
    module_ref = relative.replace(os.sep, "/")
    module_ref = re.sub(r"\.(tsx?|jsx?|mjs|cjs)$", "", module_ref, flags=re.I)
    if not module_ref.startswith("."):
        module_ref = "./" + module_ref
    return module_ref


def build_source_import_statement(
    language: str,
    source_path: Path,
    target_path: Path,
    symbols: Sequence[SymbolBlock],
) -> str:
    module_ref = compute_module_reference(source_path, target_path, language)
    if language == "python":
        names = ", ".join(symbol.name for symbol in symbols)
        return f"from {module_ref} import {names}\n"
    names = ", ".join(symbol.name for symbol in symbols)
    return f'import {{ {names} }} from "{module_ref}";\n'


def remove_line_ranges(lines: Sequence[str], ranges: Sequence[tuple[int, int]]) -> str:
    mask = [False] * (len(lines) + 1)
    for start_line, end_line in ranges:
        for line_no in range(start_line, end_line + 1):
            if 1 <= line_no <= len(lines):
                mask[line_no] = True
    remaining = [line for index, line in enumerate(lines, start=1) if not mask[index]]
    return normalize_blank_runs("".join(remaining))


def insert_import_into_source(
    module: ModuleParseResult,
    source_content: str,
    import_statement: str,
) -> str:
    if not import_statement.strip():
        return source_content
    current_lines = source_content.splitlines(keepends=True)
    last_import_line = 0
    for import_stmt in module.imports:
        if import_stmt.end_line > last_import_line:
            last_import_line = import_stmt.end_line

    if last_import_line == 0:
        current_lines.insert(0, import_statement)
        current_lines.insert(1, "\n")
    else:
        insertion_index = min(last_import_line, len(current_lines))
        current_lines.insert(insertion_index, import_statement)
        if insertion_index < len(current_lines) and current_lines[insertion_index].strip():
            current_lines.insert(insertion_index + 1, "\n")
    return normalize_blank_runs("".join(current_lines))


def build_source_content_after_move(
    module: ModuleParseResult,
    selected_symbols: Sequence[SymbolBlock],
    dependency_symbols: Sequence[SymbolBlock],
    insert_source_import: bool,
    target_path: Path,
) -> str:
    ranges = [
        (symbol.start_line, symbol.end_line)
        for symbol in [*selected_symbols, *dependency_symbols]
    ]
    source_content = remove_line_ranges(module.lines, ranges)
    if insert_source_import:
        import_statement = build_source_import_statement(
            module.language, module.path, target_path, selected_symbols
        )
        source_content = insert_import_into_source(module, source_content, import_statement)
    return source_content


def build_report(
    module: ModuleParseResult,
    selected_symbols: Sequence[SymbolBlock],
    dependency_symbols: Sequence[SymbolBlock],
    used_imports: Sequence[ImportStatement],
    unused_imports: Sequence[ImportStatement],
    unresolved: Sequence[str],
    misses: Sequence[str],
    target_path: Path,
    mode: str,
) -> dict:
    return {
        "source": str(module.path),
        "target": str(target_path),
        "language": module.language,
        "parser": module.parser,
        "mode": mode,
        "selected_symbols": [
            {
                "name": symbol.name,
                "kind": symbol.kind,
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
                "matched_by": symbol.matched_by,
                "source": symbol.source,
            }
            for symbol in selected_symbols
        ],
        "dependency_symbols": [
            {
                "name": symbol.name,
                "kind": symbol.kind,
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
                "matched_by": symbol.matched_by,
                "source": symbol.source,
            }
            for symbol in dependency_symbols
        ],
        "used_imports": [
            {
                "module": import_stmt.module,
                "local_names": import_stmt.local_names,
                "start_line": import_stmt.start_line,
                "end_line": import_stmt.end_line,
            }
            for import_stmt in used_imports
        ],
        "unused_imports": [
            {
                "module": import_stmt.module,
                "local_names": import_stmt.local_names,
                "start_line": import_stmt.start_line,
                "end_line": import_stmt.end_line,
            }
            for import_stmt in unused_imports
        ],
        "unresolved_identifiers": list(unresolved),
        "selection_misses": list(misses),
    }


def create_plan_from_values(
    *,
    module: ModuleParseResult,
    source_path: Path,
    target_path: Path,
    exact_names: Sequence[str],
    intent_queries: Sequence[str],
    include_local_deps: bool,
    keep_side_effect_imports: bool,
    insert_source_import: bool,
    mode: str,
    bucket: str | None = None,
    wrapper_kind: str | None = None,
    wrapper_name: str | None = None,
    wrapper_params: Sequence[str] | None = None,
    wrapper_returns: Sequence[str] | None = None,
    replacement_text: str | None = None,
) -> SplitPlan:
    if not exact_names and not intent_queries:
        raise SplitModuleError(
            "Nothing selected. Use --symbols and/or --what-should-be-split."
        )

    selected_symbols, misses = select_symbols(module.symbols, exact_names, intent_queries)
    if not selected_symbols:
        raise SplitModuleError(
            "No matching symbols or marker regions were found.\n"
            f"Available top-level items: {', '.join(symbol.name for symbol in module.symbols[:50])}"
        )

    import_names = {name for import_stmt in module.imports for name in import_stmt.local_names}
    dependency_symbols = resolve_local_dependencies(
        language=module.language,
        selected_symbols=selected_symbols,
        all_symbols=module.symbols,
        import_names=import_names,
        include_local_deps=include_local_deps,
        filename_hint=module.path.name,
    )
    used_imports, unused_imports = choose_used_imports(
        module, selected_symbols, dependency_symbols, keep_side_effect_imports=keep_side_effect_imports
    )
    unresolved = unresolved_identifiers(module, selected_symbols, dependency_symbols, used_imports)
    closure = analyze_closure_context(module, selected_symbols, dependency_symbols, used_imports)

    spec = SplitTargetSpec(
        path=target_path,
        symbols=list(exact_names),
        queries=list(intent_queries),
        include_local_deps=include_local_deps,
        insert_source_import=insert_source_import,
        keep_side_effect_imports=keep_side_effect_imports,
        wrapper_kind=wrapper_kind,
        wrapper_name=wrapper_name,
        wrapper_params=list(wrapper_params or []),
        wrapper_returns=list(wrapper_returns or []),
        replace_with=replacement_text,
        bucket=bucket,
    )
    if wrapper_kind:
        target_content = build_wrapper_content(
            module=module,
            spec=spec,
            selected_symbols=selected_symbols,
            dependency_symbols=dependency_symbols,
            imports=used_imports,
            closure=closure,
        )
    else:
        add_named_exports = insert_source_import and module.language == "js-like"
        target_content = build_target_content(
            module=module,
            selected_symbols=selected_symbols,
            dependency_symbols=dependency_symbols,
            used_imports=used_imports,
            add_named_exports=add_named_exports,
        )

    source_content_after_move = None
    replacement_for_source = maybe_build_region_replacement(spec, selected_symbols, closure)
    if mode == "move" and replacement_for_source is None:
        source_content_after_move = build_source_content_after_move(
            module=module,
            selected_symbols=selected_symbols,
            dependency_symbols=dependency_symbols,
            insert_source_import=insert_source_import,
            target_path=target_path,
        )

    report = build_report(
        module=module,
        selected_symbols=selected_symbols,
        dependency_symbols=dependency_symbols,
        used_imports=used_imports,
        unused_imports=unused_imports,
        unresolved=unresolved,
        misses=misses,
        target_path=target_path,
        mode=mode,
    )
    report["closure_analysis"] = asdict(closure)
    if replacement_for_source is not None:
        report["replacement_text"] = replacement_for_source

    return SplitPlan(
        source_path=source_path,
        target_path=target_path,
        language=module.language,
        parser=module.parser,
        selected_symbols=selected_symbols,
        dependency_symbols=dependency_symbols,
        used_imports=used_imports,
        unused_selected_imports=unused_imports,
        target_content=target_content,
        source_content_after_move=source_content_after_move,
        unresolved_identifiers=unresolved,
        selection_misses=misses,
        report=report,
        closure_analysis=asdict(closure),
        bucket=bucket,
        replacement_text=replacement_for_source,
    )


def create_plan(args: argparse.Namespace) -> SplitPlan:
    if not args.source or not args.target:
        raise SplitModuleError("Single-target mode requires --filename and --save-as.")
    source_path = Path(args.source).resolve()
    target_path = Path(args.target).resolve()

    if not source_path.exists():
        raise SplitModuleError(f"Source file does not exist: {source_path}")
    if source_path == target_path:
        raise SplitModuleError("Source and target paths must be different.")

    language = detect_language(source_path, args.language)
    module = parse_module(source_path, language)

    return create_plan_from_values(
        module=module,
        source_path=source_path,
        target_path=target_path,
        exact_names=parse_csvish(args.symbols),
        intent_queries=parse_csvish(args.what_should_be_split),
        include_local_deps=args.include_local_deps,
        keep_side_effect_imports=args.keep_side_effect_imports,
        insert_source_import=args.insert_source_import,
        mode=args.mode,
    )


def source_import_names_for_plan(plan: SplitPlan, spec: SplitTargetSpec) -> list[str]:
    if spec.wrapper_kind and spec.wrapper_name:
        return [spec.wrapper_name]
    return [symbol.name for symbol in plan.selected_symbols]


def refresh_split_plan(
    module: ModuleParseResult,
    plan: SplitPlan,
    spec: SplitTargetSpec,
    dependency_symbols: Sequence[SymbolBlock],
    cross_target_imports: Sequence[str],
) -> SplitPlan:
    used_imports, unused_imports = choose_used_imports(
        module,
        plan.selected_symbols,
        dependency_symbols,
        keep_side_effect_imports=spec.keep_side_effect_imports,
    )
    closure = analyze_closure_context(module, plan.selected_symbols, dependency_symbols, used_imports)
    if spec.wrapper_kind:
        target_content = build_wrapper_content(
            module=module,
            spec=spec,
            selected_symbols=plan.selected_symbols,
            dependency_symbols=dependency_symbols,
            imports=used_imports,
            closure=closure,
        )
    else:
        target_content = build_target_content(
            module=module,
            selected_symbols=plan.selected_symbols,
            dependency_symbols=dependency_symbols,
            used_imports=used_imports,
            add_named_exports=spec.insert_source_import and module.language == "js-like",
        )
    if cross_target_imports:
        target_content = prepend_imports_to_content(target_content, cross_target_imports, module.language)

    unresolved = [
        name
        for name in unresolved_identifiers(module, plan.selected_symbols, dependency_symbols, used_imports)
        if name not in closure.import_dependencies
    ]
    replacement = maybe_build_region_replacement(spec, plan.selected_symbols, closure)
    plan.dependency_symbols = list(sorted(dependency_symbols, key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.name)))
    plan.used_imports = list(used_imports)
    plan.unused_selected_imports = list(unused_imports)
    plan.target_content = target_content
    plan.unresolved_identifiers = unresolved
    plan.closure_analysis = asdict(closure)
    plan.cross_target_imports = list(cross_target_imports)
    plan.replacement_text = replacement
    plan.report["closure_analysis"] = asdict(closure)
    plan.report["cross_target_imports"] = list(cross_target_imports)
    if replacement is not None:
        plan.report["replacement_text"] = replacement
    return plan


def parse_wrapper_config(value: object) -> tuple[str | None, str | None, list[str], list[str]]:
    if value is None:
        return None, None, [], []
    if isinstance(value, str):
        return value, None, [], []
    if not isinstance(value, dict):
        raise SplitModuleError("Expected object or string for target.wrapper.")
    kind = value.get("kind")
    name = value.get("name")
    if kind is not None and not isinstance(kind, str):
        raise SplitModuleError("Expected wrapper.kind to be a string.")
    if name is not None and not isinstance(name, str):
        raise SplitModuleError("Expected wrapper.name to be a string.")
    params = coerce_string_list(value.get("params"), "target.wrapper.params")
    returns = coerce_string_list(value.get("returns"), "target.wrapper.returns")
    return kind, name, params, returns


def parse_target_spec(
    value: object,
    *,
    base_dir: Path,
    source_path: Path,
    language: str,
    default_include_local_deps: bool,
    default_insert_source_import: bool,
    default_keep_side_effect_imports: bool,
    auto_target_root: Path | None,
) -> SplitTargetSpec:
    if not isinstance(value, dict):
        raise SplitModuleError("Each target in a split plan must be an object.")
    path_value = value.get("path") or value.get("target") or value.get("saveAs")
    bucket = value.get("bucket")
    if bucket is not None and not isinstance(bucket, str):
        raise SplitModuleError("Expected target.bucket to be a string.")
    path = resolve_maybe_relative_path(path_value, base_dir) if isinstance(path_value, str) else None
    wrapper_kind, wrapper_name, wrapper_params, wrapper_returns = parse_wrapper_config(value.get("wrapper"))
    spec = SplitTargetSpec(
        path=path,
        symbols=coerce_string_list(value.get("symbols"), "target.symbols"),
        queries=coerce_string_list(value.get("queries") or value.get("whatShouldBeSplit"), "target.queries"),
        regions=coerce_string_list(value.get("regions"), "target.regions"),
        include_local_deps=bool(value.get("includeLocalDeps", default_include_local_deps)),
        insert_source_import=bool(value.get("insertSourceImport", default_insert_source_import)),
        keep_side_effect_imports=bool(value.get("keepSideEffectImports", default_keep_side_effect_imports)),
        wrapper_kind=wrapper_kind,
        wrapper_name=wrapper_name,
        wrapper_params=wrapper_params,
        wrapper_returns=wrapper_returns,
        replace_with=value.get("replaceWith") if isinstance(value.get("replaceWith"), str) else None,
        bucket=bucket,
        name=value.get("name") if isinstance(value.get("name"), str) else None,
    )
    if spec.path is None:
        inferred_bucket = spec.bucket or "chat-helpers"
        spec.bucket = inferred_bucket
        spec = apply_auto_target_path(spec, source_path, language, auto_target_root)
    return spec


def load_batch_plan_config(plan_path: Path, args: argparse.Namespace) -> BatchPlanConfig:
    payload = read_json(plan_path)
    if not isinstance(payload, dict):
        raise SplitModuleError("Split plan root must be a JSON object.")

    plan_base_dir = plan_path.parent
    workspace_root_value = payload.get("workspaceRoot")
    workspace_root = (
        resolve_maybe_relative_path(workspace_root_value, Path.cwd().resolve())
        if isinstance(workspace_root_value, str)
        else Path.cwd().resolve()
    )
    source_value = payload.get("source") or payload.get("filename") or args.source
    if not isinstance(source_value, str):
        raise SplitModuleError("Split plan must include a string 'source'.")
    source_path = resolve_maybe_relative_path(source_value, workspace_root)
    language = payload.get("language") if isinstance(payload.get("language"), str) else args.language
    detected_language = detect_language(source_path, language)
    auto_target_root_value = payload.get("autoTargetRoot")
    auto_target_root = (
        resolve_maybe_relative_path(auto_target_root_value, workspace_root)
        if isinstance(auto_target_root_value, str)
        else (resolve_maybe_relative_path(args.auto_target_root, workspace_root) if args.auto_target_root else None)
    )
    shared_target_dir_value = payload.get("sharedTargetDir")
    shared_target_dir = (
        resolve_maybe_relative_path(shared_target_dir_value, workspace_root)
        if isinstance(shared_target_dir_value, str)
        else (resolve_maybe_relative_path(args.shared_target_dir, workspace_root) if args.shared_target_dir else None)
    )
    verify_cwd_value = payload.get("verifyCwd")
    verify_cwd = (
        resolve_maybe_relative_path(verify_cwd_value, workspace_root)
        if isinstance(verify_cwd_value, str)
        else workspace_root
    )
    mode_value = payload.get("mode") if isinstance(payload.get("mode"), str) else args.mode
    if mode_value not in {"preview", "copy", "move"}:
        raise SplitModuleError(f"Unsupported batch mode: {mode_value}")

    targets_payload = payload.get("targets")
    if not isinstance(targets_payload, list) or not targets_payload:
        raise SplitModuleError("Split plan must include a non-empty 'targets' array.")

    default_include_local_deps = bool(payload.get("includeLocalDeps", args.include_local_deps))
    default_insert_source_import = bool(payload.get("insertSourceImport", args.insert_source_import))
    default_keep_side_effect_imports = bool(payload.get("keepSideEffectImports", args.keep_side_effect_imports))
    targets = [
        parse_target_spec(
            target_payload,
            base_dir=workspace_root,
            source_path=source_path,
            language=detected_language,
            default_include_local_deps=default_include_local_deps,
            default_insert_source_import=default_insert_source_import,
            default_keep_side_effect_imports=default_keep_side_effect_imports,
            auto_target_root=auto_target_root,
        )
        for target_payload in targets_payload
    ]
    verify_commands = coerce_string_list(payload.get("verifyCommands"), "verifyCommands")
    verify_commands.extend(args.verify_command or [])

    return BatchPlanConfig(
        source_path=source_path,
        mode=mode_value,
        workspace_root=workspace_root,
        language=detected_language,
        verify_commands=verify_commands,
        verify_cwd=verify_cwd,
        shared_target_dir=shared_target_dir,
        promote_shared_dependencies=bool(payload.get("promoteSharedDependencies", args.promote_shared_dependencies)),
        promote_shared_types=bool(payload.get("promoteSharedTypes", args.promote_shared_types)),
        rewrite_imports=bool(payload.get("rewriteImports", args.rewrite_imports)),
        auto_target_root=auto_target_root,
        targets=targets,
    )


def build_symbol_owner_map(
    plans_by_key: dict[str, tuple[SplitTargetSpec, SplitPlan]],
) -> dict[str, str]:
    owners: dict[str, str] = {}
    explicit_order = list(plans_by_key.items())
    for _, (_, plan) in explicit_order:
        for symbol in plan.selected_symbols:
            owners.setdefault(symbol.name, str(plan.target_path))
    for _, (_, plan) in explicit_order:
        for symbol in plan.dependency_symbols:
            owners.setdefault(symbol.name, str(plan.target_path))
    return owners


def collect_promotable_symbol_names(
    module: ModuleParseResult,
    plans_by_key: dict[str, tuple[SplitTargetSpec, SplitPlan]],
    config: BatchPlanConfig,
    owner_map: dict[str, str],
) -> list[str]:
    if not config.promote_shared_dependencies and not config.promote_shared_types:
        return []
    module_symbols = {symbol.name: symbol for symbol in module.symbols}
    dependency_counter: Counter[str] = Counter()
    for _, (_, plan) in plans_by_key.items():
        closure = ClosureAnalysis(**plan.closure_analysis) if plan.closure_analysis else ClosureAnalysis()
        for name in closure.module_symbol_dependencies:
            if name not in owner_map:
                dependency_counter[name] += 1
    promotable: list[str] = []
    for name, count in dependency_counter.items():
        symbol = module_symbols.get(name)
        if symbol is None:
            continue
        if config.promote_shared_types and symbol.kind in {"type", "interface", "enum"}:
            promotable.append(name)
            continue
        if config.promote_shared_dependencies and count > 1:
            promotable.append(name)
    return sorted(dict.fromkeys(promotable))


def build_shared_target_spec(config: BatchPlanConfig, module: ModuleParseResult, names: Sequence[str]) -> SplitTargetSpec | None:
    if not names:
        return None
    shared_dir = config.shared_target_dir or (config.workspace_root / "split-shared")
    extension = default_extension_for_language(module.language, config.source_path)
    filename = f"{config.source_path.stem}.shared{extension}"
    return SplitTargetSpec(
        path=(shared_dir / filename).resolve(),
        symbols=list(names),
        include_local_deps=False,
        insert_source_import=False,
        keep_side_effect_imports=True,
        bucket="shared",
        name="shared",
    )


def topological_sort_dependency_graph(graph: dict[str, list[str]]) -> list[str]:
    indegree: dict[str, int] = {node: 0 for node in graph}
    for node, deps in graph.items():
        for dep in deps:
            indegree[node] = indegree.get(node, 0)
            indegree[dep] = indegree.get(dep, 0)
    reverse_graph: dict[str, list[str]] = defaultdict(list)
    for node, deps in graph.items():
        indegree[node] = len(deps)
        for dep in deps:
            reverse_graph[dep].append(node)
    queue = deque(sorted(node for node, value in indegree.items() if value == 0))
    ordered: list[str] = []
    while queue:
        node = queue.popleft()
        ordered.append(node)
        for follower in sorted(reverse_graph.get(node, [])):
            indegree[follower] -= 1
            if indegree[follower] == 0:
                queue.append(follower)
    if len(ordered) != len(indegree):
        return sorted(graph)
    return ordered


def apply_rewrite_operations(lines: Sequence[str], operations: Sequence[RewriteOperation]) -> str:
    if not operations:
        return normalize_blank_runs("".join(lines))
    output_lines = list(lines)
    sorted_ops = sorted(operations, key=lambda op: (op.start_line, op.end_line), reverse=True)
    for operation in sorted_ops:
        replacement_lines = [] if not operation.replacement else [
            line + "\n" for line in operation.replacement.rstrip("\n").splitlines()
        ]
        output_lines[operation.start_line - 1 : operation.end_line] = replacement_lines
    return normalize_blank_runs("".join(output_lines))


def insert_multiple_imports_into_source(
    module: ModuleParseResult,
    source_content: str,
    import_statements: Sequence[str],
) -> str:
    if not import_statements:
        return source_content
    content = source_content
    for statement in dedupe_import_block([statement.rstrip() for statement in import_statements], module.language):
        content = insert_import_into_source(module, content, statement + "\n")
    return content


def build_batch_source_content_after_move(
    module: ModuleParseResult,
    plan_pairs: Sequence[tuple[SplitTargetSpec, SplitPlan]],
) -> str:
    operations: list[RewriteOperation] = []
    import_statements: list[str] = []
    for spec, plan in plan_pairs:
        selected_ranges = [(symbol.start_line, symbol.end_line) for symbol in plan.selected_symbols]
        dependency_ranges = [(symbol.start_line, symbol.end_line) for symbol in plan.dependency_symbols]
        if plan.replacement_text is not None and selected_ranges:
            first_start, first_end = selected_ranges[0]
            operations.append(
                RewriteOperation(
                    start_line=first_start,
                    end_line=first_end,
                    replacement=plan.replacement_text,
                    description=f"replace {spec.name or plan.target_path.name}",
                )
            )
            for start_line, end_line in selected_ranges[1:] + dependency_ranges:
                operations.append(RewriteOperation(start_line=start_line, end_line=end_line))
        else:
            for start_line, end_line in selected_ranges + dependency_ranges:
                operations.append(RewriteOperation(start_line=start_line, end_line=end_line))
            if spec.insert_source_import:
                import_names = source_import_names_for_plan(plan, spec)
                import_statements.append(
                    build_cross_target_import_statement(module.language, module.path, plan.target_path, import_names)
                )
    source_content = apply_rewrite_operations(module.lines, operations)
    return insert_multiple_imports_into_source(module, source_content, import_statements)


def run_verification_commands(commands: Sequence[str], cwd: Path) -> list[dict]:
    results: list[dict] = []
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        results.append(
            {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout[-8000:],
                "stderr": completed.stderr[-8000:],
                "ok": completed.returncode == 0,
            }
        )
    return results


def create_batch_plan(config: BatchPlanConfig) -> BatchSplitPlan:
    if not config.source_path.exists():
        raise SplitModuleError(f"Source file does not exist: {config.source_path}")
    module = parse_module(config.source_path, detect_language(config.source_path, config.language))
    plan_pairs: list[tuple[SplitTargetSpec, SplitPlan]] = []
    for spec in config.targets:
        if spec.path is None:
            raise SplitModuleError("Each batch target must resolve to a path.")
        exact_names = [*spec.symbols, *spec.regions]
        plan = create_plan_from_values(
            module=module,
            source_path=config.source_path,
            target_path=spec.path,
            exact_names=exact_names,
            intent_queries=spec.queries,
            include_local_deps=spec.include_local_deps,
            keep_side_effect_imports=spec.keep_side_effect_imports,
            insert_source_import=spec.insert_source_import,
            mode=config.mode,
            bucket=spec.bucket,
            wrapper_kind=spec.wrapper_kind,
            wrapper_name=spec.wrapper_name,
            wrapper_params=spec.wrapper_params,
            wrapper_returns=spec.wrapper_returns,
            replacement_text=spec.replace_with,
        )
        if plan.selected_symbols:
            suggested_bucket = spec.bucket or bucketize_symbol(plan.selected_symbols[0])
            if spec.bucket is None:
                plan.bucket = suggested_bucket
                if config.auto_target_root and spec.path.parent == config.auto_target_root:
                    extension = spec.path.suffix or default_extension_for_language(module.language, config.source_path)
                    base_name = spec.path.stem
                    spec.path = (config.auto_target_root / suggested_bucket / f"{base_name}{extension}").resolve()
                    plan = create_plan_from_values(
                        module=module,
                        source_path=config.source_path,
                        target_path=spec.path,
                        exact_names=exact_names,
                        intent_queries=spec.queries,
                        include_local_deps=spec.include_local_deps,
                        keep_side_effect_imports=spec.keep_side_effect_imports,
                        insert_source_import=spec.insert_source_import,
                        mode=config.mode,
                        bucket=suggested_bucket,
                        wrapper_kind=spec.wrapper_kind,
                        wrapper_name=spec.wrapper_name,
                        wrapper_params=spec.wrapper_params,
                        wrapper_returns=spec.wrapper_returns,
                        replacement_text=spec.replace_with,
                    )
            else:
                plan.bucket = suggested_bucket
        plan_pairs.append((spec, plan))

    plans_by_key: dict[str, tuple[SplitTargetSpec, SplitPlan]] = {
        str(plan.target_path): (spec, plan) for spec, plan in plan_pairs
    }
    owner_map = build_symbol_owner_map(plans_by_key)
    promotable_names = collect_promotable_symbol_names(module, plans_by_key, config, owner_map)
    shared_spec = build_shared_target_spec(config, module, promotable_names)
    if shared_spec is not None and shared_spec.path is not None and str(shared_spec.path) not in plans_by_key:
        shared_plan = create_plan_from_values(
            module=module,
            source_path=config.source_path,
            target_path=shared_spec.path,
            exact_names=shared_spec.symbols,
            intent_queries=[],
            include_local_deps=False,
            keep_side_effect_imports=True,
            insert_source_import=False,
            mode=config.mode,
            bucket="shared",
        )
        plan_pairs.append((shared_spec, shared_plan))
        plans_by_key[str(shared_plan.target_path)] = (shared_spec, shared_plan)
        owner_map = build_symbol_owner_map(plans_by_key)

    dependency_graph: dict[str, list[str]] = {}
    for path_key, (spec, plan) in plans_by_key.items():
        closure = ClosureAnalysis(**plan.closure_analysis) if plan.closure_analysis else ClosureAnalysis()
        kept_dependencies: list[SymbolBlock] = []
        cross_target_names: dict[str, list[str]] = defaultdict(list)
        for dependency in plan.dependency_symbols:
            owner = owner_map.get(dependency.name)
            if owner and owner != path_key and config.rewrite_imports:
                cross_target_names[owner].append(dependency.name)
            else:
                kept_dependencies.append(dependency)
        for name in closure.module_symbol_dependencies:
            owner = owner_map.get(name)
            if owner and owner != path_key and config.rewrite_imports:
                cross_target_names[owner].append(name)
        cross_imports = [
            build_cross_target_import_statement(
                module.language,
                plan.target_path,
                Path(owner_path),
                sorted(dict.fromkeys(names)),
            )
            for owner_path, names in sorted(cross_target_names.items())
        ]
        dependency_graph[path_key] = sorted(cross_target_names.keys())
        refresh_split_plan(module, plan, spec, kept_dependencies, cross_imports)

    execution_order = topological_sort_dependency_graph(dependency_graph)
    source_content_after_move = None
    if config.mode == "move":
        source_content_after_move = build_batch_source_content_after_move(module, plan_pairs)
    report = {
        "source": str(config.source_path),
        "mode": config.mode,
        "targets": [plan.report for _, plan in plan_pairs],
        "dependency_graph": dependency_graph,
        "execution_order": execution_order,
    }
    return BatchSplitPlan(
        config=config,
        module=module,
        target_plans=[plan for _, plan in plan_pairs],
        source_content_after_move=source_content_after_move,
        dependency_graph=dependency_graph,
        execution_order=execution_order,
        verification_results=[],
        report=report,
    )


def apply_batch_plan(batch_plan: BatchSplitPlan, args: argparse.Namespace) -> list[dict]:
    plans_by_path = {str(plan.target_path): plan for plan in batch_plan.target_plans}
    if args.create_backup:
        for plan in batch_plan.target_plans:
            maybe_backup(plan.target_path)
        if batch_plan.config.mode == "move":
            maybe_backup(batch_plan.config.source_path)
    for path_key in batch_plan.execution_order:
        plan = plans_by_path[path_key]
        write_text(plan.target_path, plan.target_content, force=args.force)
    if batch_plan.config.mode == "move" and batch_plan.source_content_after_move is not None:
        write_text(batch_plan.config.source_path, batch_plan.source_content_after_move, force=True)
    if batch_plan.config.verify_commands:
        return run_verification_commands(
            batch_plan.config.verify_commands,
            batch_plan.config.verify_cwd or batch_plan.config.workspace_root,
        )
    return []


def format_batch_console_report(batch_plan: BatchSplitPlan) -> str:
    lines = [
        f"Source: {batch_plan.config.source_path}",
        f"Mode: {batch_plan.config.mode}",
        f"Targets: {len(batch_plan.target_plans)}",
        "",
        "Execution order:",
    ]
    for target_path in batch_plan.execution_order:
        lines.append(f"  - {target_path}")
    for plan in batch_plan.target_plans:
        lines.append("")
        lines.append(f"Target: {plan.target_path}")
        if plan.bucket:
            lines.append(f"  Bucket: {plan.bucket}")
        lines.append(
            "  Selected: " + ", ".join(symbol.name for symbol in plan.selected_symbols)
        )
        if plan.dependency_symbols:
            lines.append(
                "  Local deps: " + ", ".join(symbol.name for symbol in plan.dependency_symbols)
            )
        if plan.cross_target_imports:
            lines.append("  Cross-target imports:")
            for statement in plan.cross_target_imports:
                lines.append(f"    - {statement}")
        closure = ClosureAnalysis(**plan.closure_analysis) if plan.closure_analysis else ClosureAnalysis()
        if closure.closure_captures:
            lines.append("  Closure captures: " + ", ".join(closure.closure_captures))
        if closure.module_symbol_dependencies:
            lines.append("  Module deps: " + ", ".join(closure.module_symbol_dependencies))
    return "\n".join(lines)


def format_console_report(plan: SplitPlan) -> str:
    lines: list[str] = []
    lines.append(f"Source: {plan.source_path}")
    lines.append(f"Target: {plan.target_path}")
    lines.append(f"Language: {plan.language}")
    lines.append(f"Parser: {plan.parser}")
    if plan.bucket:
        lines.append(f"Bucket: {plan.bucket}")
    lines.append("")
    lines.append("Selected symbols:")
    for symbol in plan.selected_symbols:
        suffix = f" matched by {', '.join(symbol.matched_by)}" if symbol.matched_by else ""
        lines.append(
            f"  - {symbol.name} ({symbol.kind}) [{symbol.start_line}:{symbol.end_line}]{suffix}"
        )
    if plan.dependency_symbols:
        lines.append("")
        lines.append("Included local dependencies:")
        for symbol in plan.dependency_symbols:
            lines.append(
                f"  - {symbol.name} ({symbol.kind}) [{symbol.start_line}:{symbol.end_line}]"
            )
    if plan.used_imports:
        lines.append("")
        lines.append("Used imports:")
        for import_stmt in plan.used_imports:
            module_label = import_stmt.module or "<unknown>"
            local_label = ", ".join(import_stmt.local_names) if import_stmt.local_names else "(side effect)"
            lines.append(
                f"  - {module_label} -> {local_label} [{import_stmt.start_line}:{import_stmt.end_line}]"
            )
    if plan.selection_misses:
        lines.append("")
        lines.append("Selection misses:")
        for miss in plan.selection_misses:
            lines.append(f"  - {miss}")
    if plan.closure_analysis:
        closure = ClosureAnalysis(**plan.closure_analysis)
        if closure.closure_captures or closure.module_symbol_dependencies:
            lines.append("")
            lines.append("Closure analysis:")
            if closure.module_symbol_dependencies:
                lines.append(
                    "  - module symbol dependencies: "
                    + ", ".join(closure.module_symbol_dependencies)
                )
            if closure.closure_captures:
                lines.append("  - closure captures: " + ", ".join(closure.closure_captures))
    if plan.unresolved_identifiers:
        lines.append("")
        lines.append("Potential unresolved identifiers:")
        for name in plan.unresolved_identifiers[:100]:
            lines.append(f"  - {name}")
        if len(plan.unresolved_identifiers) > 100:
            lines.append(f"  - ... and {len(plan.unresolved_identifiers) - 100} more")
    return "\n".join(lines)


def apply_plan(plan: SplitPlan, args: argparse.Namespace) -> None:
    if args.create_backup:
        maybe_backup(plan.target_path)
        if args.mode == "move":
            maybe_backup(plan.source_path)

    write_text(plan.target_path, plan.target_content, force=args.force)

    if args.mode == "move":
        if plan.source_content_after_move is None:
            raise SplitModuleError("Internal error: move requested without source rewrite.")
        write_text(plan.source_path, plan.source_content_after_move, force=True)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Split one source module into a separate target file by symbol, marker, or best-effort intent query.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Selection tips:
  --symbols NameA,NameB
  --what-should-be-split "intent phrase, another phrase"
  --what-should-be-split kind:function
  --what-should-be-split name:WorkbenchTitlebar

Marker regions:
  // @split-start Name
  ...
  // @split-end Name

Safe workflow:
  1. Run with --mode preview --print-target
  2. Inspect unresolved identifiers in the report
  3. Re-run with --mode move or --mode copy

Batch plan example:
  python tools/split_module.py --plan split-plan.json --mode move --verify-command "npm run build"
""".rstrip(),
    )
    parser.add_argument(
        "--plan",
        help="JSON batch split plan. When provided, the single-target flags become optional.",
    )
    parser.add_argument("--filename", "--source", dest="source", help="Source file to split.")
    parser.add_argument("--save-as", "--target", dest="target", help="Target file path to write.")
    parser.add_argument(
        "--symbols",
        nargs="*",
        help="Exact symbol names to extract. Comma-separated values are accepted.",
    )
    parser.add_argument(
        "--what-should-be-split",
        nargs="*",
        help="Best-effort intent queries. Comma-separated values are accepted.",
    )
    parser.add_argument(
        "--mode",
        choices=("preview", "copy", "move"),
        default=DEFAULT_MODE,
        help="preview only, copy to target, or move to target and rewrite source.",
    )
    parser.add_argument(
        "--language",
        help="Override language detection. Supported: js-like, ts, tsx, js, jsx, python.",
    )
    parser.add_argument(
        "--include-local-deps",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Also pull in referenced top-level symbols from the same source file.",
    )
    parser.add_argument(
        "--insert-source-import",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="After moving content, import the extracted symbols back into the source file.",
    )
    parser.add_argument(
        "--keep-side-effect-imports",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep side-effect-only imports in the target when they exist.",
    )
    parser.add_argument(
        "--allow-unresolved",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow the plan to continue even if unresolved identifiers are detected.",
    )
    parser.add_argument(
        "--force",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Allow overwriting the target file.",
    )
    parser.add_argument(
        "--create-backup",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Create .bak copies before writing modified files.",
    )
    parser.add_argument(
        "--print-target",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Print the generated target content to stdout.",
    )
    parser.add_argument(
        "--print-source-after-move",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Print the rewritten source content in move mode.",
    )
    parser.add_argument(
        "--json-report",
        dest="json_report",
        help="Write a JSON report to the given path.",
    )
    parser.add_argument(
        "--verify-command",
        action="append",
        help="Command to run after apply, or after plan load in batch mode. Can be repeated.",
    )
    parser.add_argument(
        "--shared-target-dir",
        help="Directory for promoted shared dependencies/types in batch mode.",
    )
    parser.add_argument(
        "--auto-target-root",
        help="Root directory for auto-generated target paths in batch mode when targets omit 'path'.",
    )
    parser.add_argument(
        "--promote-shared-dependencies",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="In batch mode, promote cross-target shared dependencies used by multiple targets.",
    )
    parser.add_argument(
        "--promote-shared-types",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="In batch mode, promote reused interfaces/types/enums into a shared target.",
    )
    parser.add_argument(
        "--rewrite-imports",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="In batch mode, synthesize cross-target imports and prune duplicated local deps.",
    )
    parser.add_argument(
        "--print-batch-source",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="In batch mode, print the source file after all move rewrites.",
    )
    parser.add_argument(
        "--print-batch-targets",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="In batch mode, print every generated target content.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.plan:
        try:
            batch_plan = create_batch_plan(load_batch_plan_config(Path(args.plan).resolve(), args))
        except SplitModuleError as exc:
            eprint(f"Error: {exc}")
            return 2

        report_text = format_batch_console_report(batch_plan)
        print(report_text)

        if args.json_report:
            report_path = Path(args.json_report).resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(batch_plan.report, indent=2), encoding="utf-8")

        if args.print_batch_targets:
            for plan in batch_plan.target_plans:
                print(f"\n=== TARGET: {plan.target_path} ===\n")
                print(plan.target_content)

        if batch_plan.config.mode == "move" and args.print_batch_source and batch_plan.source_content_after_move is not None:
            print("\n=== SOURCE AFTER BATCH MOVE ===\n")
            print(batch_plan.source_content_after_move)

        unresolved = {
            path.name: plan.unresolved_identifiers
            for path, plan in ((Path(plan.target_path), plan) for plan in batch_plan.target_plans)
            if plan.unresolved_identifiers
        }
        if unresolved and not args.allow_unresolved:
            eprint("\nAborting because unresolved identifiers were detected and --allow-unresolved is false.")
            return 3

        if batch_plan.config.mode == "preview":
            return 0

        try:
            verification_results = apply_batch_plan(batch_plan, args)
        except SplitModuleError as exc:
            eprint(f"Error: {exc}")
            return 4

        if verification_results:
            print("\nVerification results:")
            for result in verification_results:
                status = "ok" if result["ok"] else f"failed ({result['returncode']})"
                print(f"  - {result['command']}: {status}")
        print(f"\nApplied batch split for {len(batch_plan.target_plans)} target(s).")
        return 0

    try:
        plan = create_plan(args)
    except SplitModuleError as exc:
        eprint(f"Error: {exc}")
        return 2

    report_text = format_console_report(plan)
    print(report_text)

    if args.json_report:
        report_path = Path(args.json_report).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(plan.report, indent=2), encoding="utf-8")

    if plan.unresolved_identifiers and not args.allow_unresolved:
        eprint(
            "\nAborting because unresolved identifiers were detected and "
            "--allow-unresolved is false."
        )
        return 3

    if args.print_target:
        print("\n=== TARGET CONTENT ===\n")
        print(plan.target_content)

    if args.mode == "move" and args.print_source_after_move and plan.source_content_after_move is not None:
        print("\n=== SOURCE AFTER MOVE ===\n")
        print(plan.source_content_after_move)

    if args.mode == "preview":
        return 0

    try:
        apply_plan(plan, args)
    except SplitModuleError as exc:
        eprint(f"Error: {exc}")
        return 4

    action = "Copied" if args.mode == "copy" else "Moved"
    print(f"\n{action} {len(plan.selected_symbols)} symbol(s) to {plan.target_path}")
    if plan.dependency_symbols:
        print(f"Also included {len(plan.dependency_symbols)} local dependency symbol(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
