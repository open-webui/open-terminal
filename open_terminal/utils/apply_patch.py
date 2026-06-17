from dataclasses import dataclass, field
from typing import Optional

from open_terminal.utils.fs import UserFS


@dataclass
class PatchChunk:
    context: Optional[str]
    old_lines: list[str]
    new_lines: list[str]


@dataclass
class ParsedPatchChange:
    type: str
    path: str
    content: Optional[str] = None
    chunks: list[PatchChunk] = field(default_factory=list)
    move_path: Optional[str] = None


@dataclass
class StagedPatchChange:
    type: str
    path: str
    new_content: Optional[str] = None
    move_path: Optional[str] = None


class PatchParseError(ValueError):
    pass


def _is_patch_hunk_marker(line: str) -> bool:
    return (
        line.startswith("*** Add File: ")
        or line.startswith("*** Delete File: ")
        or line.startswith("*** Update File: ")
    )


def _patch_lines_to_text(lines: list[str]) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def parse_apply_patch_text(patch: str) -> list[ParsedPatchChange]:
    lines = patch.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines or lines[0].strip() != "*** Begin Patch":
        raise PatchParseError("The first line of the patch must be '*** Begin Patch'")
    if len(lines) < 2 or lines[-1].strip() != "*** End Patch":
        raise PatchParseError("The last line of the patch must be '*** End Patch'")

    changes: list[ParsedPatchChange] = []
    i = 1
    end = len(lines) - 1
    while i < end:
        line = lines[i]
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: "):].strip()
            i += 1
            content_lines: list[str] = []
            while i < end and not _is_patch_hunk_marker(lines[i]):
                if not lines[i].startswith("+"):
                    raise PatchParseError(f"Invalid add-file line at patch line {i + 1}")
                content_lines.append(lines[i][1:])
                i += 1
            changes.append(
                ParsedPatchChange(
                    type="add",
                    path=path,
                    content=_patch_lines_to_text(content_lines),
                )
            )
            continue

        if line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: "):].strip()
            changes.append(ParsedPatchChange(type="delete", path=path))
            i += 1
            continue

        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: "):].strip()
            i += 1
            move_path = None
            if i < end and lines[i].startswith("*** Move to: "):
                move_path = lines[i][len("*** Move to: "):].strip()
                i += 1

            chunks: list[PatchChunk] = []
            current: Optional[PatchChunk] = None

            def flush_current():
                nonlocal current
                if current and (current.old_lines or current.new_lines):
                    chunks.append(current)
                current = None

            while i < end and not _is_patch_hunk_marker(lines[i]):
                patch_line = lines[i]
                if patch_line == "*** End of File":
                    i += 1
                    continue
                if patch_line.startswith("@@"):
                    flush_current()
                    context = patch_line[3:] if patch_line.startswith("@@ ") else None
                    current = PatchChunk(context=context, old_lines=[], new_lines=[])
                    i += 1
                    continue

                if not patch_line:
                    raise PatchParseError(f"Invalid empty patch line at patch line {i + 1}")
                prefix = patch_line[0]
                value = patch_line[1:]
                if prefix not in (" ", "+", "-"):
                    raise PatchParseError(f"Invalid update line at patch line {i + 1}")
                if current is None:
                    current = PatchChunk(context=None, old_lines=[], new_lines=[])
                if prefix == " ":
                    current.old_lines.append(value)
                    current.new_lines.append(value)
                elif prefix == "-":
                    current.old_lines.append(value)
                else:
                    current.new_lines.append(value)
                i += 1

            flush_current()
            if not chunks and move_path is None:
                raise PatchParseError(f"Update file has no changes: {path}")
            changes.append(
                ParsedPatchChange(
                    type="update",
                    path=path,
                    chunks=chunks,
                    move_path=move_path,
                )
            )
            continue

        raise PatchParseError(f"Invalid patch hunk at patch line {i + 1}")

    if not changes:
        raise PatchParseError("Patch must contain at least one hunk")
    return changes


def _replace_chunk_once(content: str, chunk: PatchChunk, start_at: int) -> tuple[str, int] | None:
    old_text = _patch_lines_to_text(chunk.old_lines)
    new_text = _patch_lines_to_text(chunk.new_lines)
    search_start = start_at

    if chunk.context:
        context_text = chunk.context
        context_index = content.find(context_text, search_start)
        if context_index == -1:
            context_index = content.find(context_text + "\n", search_start)
        if context_index == -1:
            return None
        search_start = context_index + len(context_text)

    old_index = content.find(old_text, search_start)
    matched_old = old_text
    if old_index == -1 and old_text.endswith("\n"):
        matched_old = old_text[:-1]
        old_index = content.find(matched_old, search_start)
    if old_index == -1:
        return None

    updated = content[:old_index] + new_text + content[old_index + len(matched_old):]
    return updated, old_index + len(new_text)


async def stage_apply_patch(
    changes: list[ParsedPatchChange],
    fs: UserFS,
    cwd: Optional[str],
) -> tuple[list[StagedPatchChange], list[dict]]:
    staged: list[StagedPatchChange] = []
    conflicts: list[dict] = []

    for change in changes:
        path = fs.resolve_path(change.path, cwd=cwd)

        if change.type == "add":
            if await fs.exists(path):
                conflicts.append({"path": path, "reason": "file already exists"})
                continue
            staged.append(
                StagedPatchChange(
                    type="add",
                    path=path,
                    new_content=change.content or "",
                )
            )
            continue

        if change.type == "delete":
            if not await fs.isfile(path):
                conflicts.append({"path": path, "reason": "file not found"})
                continue
            staged.append(StagedPatchChange(type="delete", path=path))
            continue

        if change.type == "update":
            if not await fs.isfile(path):
                conflicts.append({"path": path, "reason": "file not found"})
                continue
            try:
                content = await fs.read_text(path)
            except UnicodeDecodeError:
                conflicts.append({"path": path, "reason": "file is not valid UTF-8 text"})
                continue
            except OSError as e:
                conflicts.append({"path": path, "reason": str(e)})
                continue

            next_search_start = 0
            failed = False
            for chunk in change.chunks:
                replaced = _replace_chunk_once(content, chunk, next_search_start)
                if replaced is None:
                    conflicts.append({"path": path, "reason": "old content not found"})
                    failed = True
                    break
                content, next_search_start = replaced
            if failed:
                continue

            move_path = fs.resolve_path(change.move_path, cwd=cwd) if change.move_path else None
            if move_path and move_path != path and await fs.exists(move_path):
                conflicts.append({"path": move_path, "reason": "move destination already exists"})
                continue
            staged.append(
                StagedPatchChange(
                    type="update",
                    path=path,
                    move_path=move_path,
                    new_content=content,
                )
            )
            continue

        conflicts.append({"path": path, "reason": f"unsupported change type: {change.type}"})

    return staged, conflicts


async def commit_staged_patch(staged: list[StagedPatchChange], fs: UserFS):
    for change in staged:
        if change.type == "delete":
            await fs.remove(change.path)
            continue

        target = change.move_path or change.path
        await fs.write(target, change.new_content or "")
        if change.move_path and change.move_path != change.path:
            await fs.remove(change.path)
