#!/usr/bin/env python3
"""
apply_patch.py

Preview-first patch applier for the Codex-style patch format:

    *** Begin Patch
    *** Add File: path
    +content
    *** Update File: path
    @@
    -old
    +new
     context
    *** Delete File: path
    *** End Patch

Supports:
- Add file
- Delete file
- Update file
- Move file
- Preview diffs before apply

This is intentionally deterministic and local-only. It does not try to be a
general patch tool; it targets the patch format used by the built-in
`apply_patch` tool.
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


DEFAULT_ENCODING = "utf-8"
DEFAULT_MODE = "preview"


class PatchError(Exception):
    pass


@dataclass(slots=True)
class Hunk:
    header: str
    lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FileOp:
    kind: str
    path: Path
    move_to: Path | None = None
    hunks: list[Hunk] = field(default_factory=list)
    add_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FilePlan:
    kind: str
    original_path: Path
    final_path: Path | None
    original_text: str
    final_text: str
    existed_before: bool
    created: bool
    deleted: bool


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply Codex-style *** Begin Patch patches with preview-first safety."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--patch-file", help="Read patch text from a file.")
    source.add_argument("--stdin", action="store_true", help="Read patch text from stdin.")

    parser.add_argument(
        "--mode",
        choices=["preview", "apply"],
        default=DEFAULT_MODE,
        help="Preview diffs or apply changes.",
    )
    parser.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help=f"Text encoding for file reads and writes. Default: {DEFAULT_ENCODING}",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=3,
        help="Unified diff context lines in preview mode.",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Write .bak files before modifying existing files in apply mode.",
    )
    parser.add_argument(
        "--fail-on-noop",
        action="store_true",
        help="Exit with an error if the patch produces no changes.",
    )
    return parser.parse_args(argv)


def load_patch_text(args: argparse.Namespace) -> str:
    if args.patch_file:
        return Path(args.patch_file).read_text(encoding=args.encoding)
    return sys.stdin.read()


def parse_patch(text: str) -> list[FileOp]:
    lines = text.splitlines()
    if not lines or lines[0] != "*** Begin Patch":
        raise PatchError("patch must start with '*** Begin Patch'")
    if lines[-1] != "*** End Patch":
        raise PatchError("patch must end with '*** End Patch'")

    ops: list[FileOp] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if line.startswith("*** Add File: "):
            path = Path(line[len("*** Add File: ") :])
            i += 1
            add_lines: list[str] = []
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                content_line = lines[i]
                if not content_line.startswith("+"):
                    raise PatchError(f"add-file lines must start with '+': {content_line!r}")
                add_lines.append(content_line[1:])
                i += 1
            ops.append(FileOp(kind="add", path=path, add_lines=add_lines))
            continue

        if line.startswith("*** Delete File: "):
            path = Path(line[len("*** Delete File: ") :])
            ops.append(FileOp(kind="delete", path=path))
            i += 1
            continue

        if line.startswith("*** Update File: "):
            path = Path(line[len("*** Update File: ") :])
            op = FileOp(kind="update", path=path)
            i += 1
            if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                op.move_to = Path(lines[i][len("*** Move to: ") :])
                i += 1
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if not lines[i].startswith("@@"):
                    raise PatchError(f"expected hunk header, got: {lines[i]!r}")
                header = lines[i]
                i += 1
                hunk_lines: list[str] = []
                while i < len(lines) - 1 and not lines[i].startswith("@@") and not lines[i].startswith("*** "):
                    prefix = lines[i][:1]
                    if prefix not in {" ", "+", "-"}:
                        raise PatchError(f"invalid hunk line: {lines[i]!r}")
                    hunk_lines.append(lines[i])
                    i += 1
                op.hunks.append(Hunk(header=header, lines=hunk_lines))
            ops.append(op)
            continue

        if line.strip():
            raise PatchError(f"unexpected patch line: {line!r}")
        i += 1

    return ops


def read_text(path: Path, encoding: str) -> tuple[bool, str]:
    if not path.exists():
        return False, ""
    if path.is_dir():
        raise PatchError(f"path is a directory, not a file: {path}")
    return True, path.read_text(encoding=encoding)


def find_sequence(lines: list[str], needle: list[str], start: int = 0) -> int:
    if not needle:
        return start
    limit = len(lines) - len(needle) + 1
    for idx in range(start, max(start, limit)):
        if lines[idx : idx + len(needle)] == needle:
            return idx
    return -1


def apply_hunk(lines: list[str], hunk: Hunk, path: Path) -> list[str]:
    old_lines = [line[1:] for line in hunk.lines if line.startswith((" ", "-"))]
    new_lines = [line[1:] for line in hunk.lines if line.startswith((" ", "+"))]

    idx = find_sequence(lines, old_lines)
    if idx < 0:
        raise PatchError(f"failed to match hunk in {path}: {hunk.header}")

    return lines[:idx] + new_lines + lines[idx + len(old_lines) :]


def build_plans(ops: list[FileOp], encoding: str) -> list[FilePlan]:
    plans: list[FilePlan] = []
    for op in ops:
        if op.kind == "add":
            existed, original_text = read_text(op.path, encoding)
            if existed:
                raise PatchError(f"cannot add file that already exists: {op.path}")
            final_text = "\n".join(op.add_lines)
            if op.add_lines:
                final_text += "\n"
            plans.append(
                FilePlan(
                    kind="add",
                    original_path=op.path,
                    final_path=op.path,
                    original_text="",
                    final_text=final_text,
                    existed_before=False,
                    created=True,
                    deleted=False,
                )
            )
            continue

        if op.kind == "delete":
            existed, original_text = read_text(op.path, encoding)
            if not existed:
                raise PatchError(f"cannot delete missing file: {op.path}")
            plans.append(
                FilePlan(
                    kind="delete",
                    original_path=op.path,
                    final_path=None,
                    original_text=original_text,
                    final_text="",
                    existed_before=True,
                    created=False,
                    deleted=True,
                )
            )
            continue

        if op.kind == "update":
            existed, original_text = read_text(op.path, encoding)
            if not existed:
                raise PatchError(f"cannot update missing file: {op.path}")
            line_ending = "\r\n" if "\r\n" in original_text else "\n"
            original_lines = original_text.splitlines()
            updated_lines = list(original_lines)
            for hunk in op.hunks:
                updated_lines = apply_hunk(updated_lines, hunk, op.path)
            final_text = line_ending.join(updated_lines)
            if original_text.endswith(("\n", "\r\n")):
                final_text += line_ending
            plans.append(
                FilePlan(
                    kind="move" if op.move_to else "update",
                    original_path=op.path,
                    final_path=op.move_to or op.path,
                    original_text=original_text,
                    final_text=final_text,
                    existed_before=True,
                    created=False,
                    deleted=False,
                )
            )
            continue

        raise PatchError(f"unsupported operation kind: {op.kind}")
    return plans


def unified_diff(path: Path, before: str, after: str, context: int) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
        )
    )


def print_preview(plans: list[FilePlan], context: int) -> None:
    if not plans:
        print("No changes.")
        return
    for index, plan in enumerate(plans):
        if index:
            print("\n" + ("=" * 80) + "\n")
        target = plan.final_path or plan.original_path
        print(
            f"# {plan.kind} | from={plan.original_path}"
            + (f" | to={target}" if target != plan.original_path else "")
        )
        if plan.deleted:
            print(unified_diff(plan.original_path, plan.original_text, "", context))
        else:
            print(unified_diff(plan.original_path, plan.original_text, plan.final_text, context))


def apply_changes(plans: list[FilePlan], encoding: str, backup: bool) -> None:
    for plan in plans:
        if backup and plan.existed_before and not plan.deleted:
            backup_path = Path(str(plan.original_path) + ".bak")
            shutil.copyfile(plan.original_path, backup_path)

        if plan.kind == "add":
            assert plan.final_path is not None
            plan.final_path.parent.mkdir(parents=True, exist_ok=True)
            plan.final_path.write_text(plan.final_text, encoding=encoding, newline="")
            continue

        if plan.kind == "delete":
            if backup:
                backup_path = Path(str(plan.original_path) + ".bak")
                shutil.copyfile(plan.original_path, backup_path)
            plan.original_path.unlink()
            continue

        if plan.kind in {"update", "move"}:
            assert plan.final_path is not None
            plan.final_path.parent.mkdir(parents=True, exist_ok=True)
            plan.final_path.write_text(plan.final_text, encoding=encoding, newline="")
            if plan.final_path != plan.original_path:
                if backup:
                    backup_path = Path(str(plan.original_path) + ".bak")
                    shutil.copyfile(plan.original_path, backup_path)
                plan.original_path.unlink()
            continue


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        patch_text = load_patch_text(args)
        ops = parse_patch(patch_text)
        plans = build_plans(ops, args.encoding)
        if args.fail_on_noop and not any(
            plan.created or plan.deleted or plan.final_text != plan.original_text for plan in plans
        ):
            raise PatchError("patch produced no changes")

        if args.mode == "preview":
            print_preview(plans, args.context)
        else:
            apply_changes(plans, args.encoding, args.backup)
            print(f"Applied patch to {len(plans)} file(s).")
        return 0
    except PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
