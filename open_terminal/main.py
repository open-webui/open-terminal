import asyncio
from importlib.metadata import version as _pkg_version
import fnmatch
import json

import aiofiles
import aiofiles.os
import os
import platform
import re
import shutil
import signal
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from pypdf import PdfReader

from open_terminal.env import API_KEY, BINARY_FILE_MIME_PREFIXES, CORS_ALLOWED_ORIGINS, ENABLE_NOTEBOOKS, ENABLE_TERMINAL, EXECUTE_DESCRIPTION, EXECUTE_TIMEOUT, LOG_DIR, MAX_TERMINAL_SESSIONS, MULTI_USER, TERMINAL_TERM
from open_terminal.runner import PipeRunner, ProcessRunner, create_runner

if MULTI_USER:
    from open_terminal.user_isolation import (
        check_environment, resolve_user,
        sudo_list_dir, sudo_mkdir, sudo_mv, sudo_read_file, sudo_rm, sudo_write_file,
    )
    check_environment()

try:
    import fcntl
    import pty
    import struct
    import subprocess
    import termios

    _PTY_AVAILABLE = True
except ImportError:
    _PTY_AVAILABLE = False  # Windows


def get_system_info() -> str:
    """Gather runtime system metadata for the OpenAPI description."""
    shell = os.environ.get("SHELL", "/bin/sh")
    return (
        f"This system is running {platform.system()} {platform.release()} ({platform.machine()}) "
        f"on {socket.gethostname()} as user '{os.getenv('USER', 'unknown')}' with {shell}. "
        f"Python {sys.version.split()[0]} is available."
    )


_EXECUTE_DESCRIPTION = (
    "Run a shell command in the background and return a command ID.\n\n"
    + get_system_info()
)
if EXECUTE_DESCRIPTION:
    _EXECUTE_DESCRIPTION += "\n\n" + EXECUTE_DESCRIPTION

bearer_scheme = HTTPBearer(auto_error=False)


async def verify_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    if not API_KEY:
        return
    if not credentials or credentials.credentials != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def get_user_context(request: Request) -> tuple[str | None, str | None]:
    """Resolve the OS user context for multi-user mode.

    Reads the ``X-User-Id`` header (set by the OWUI proxy) and provisions
    a per-user Linux account if needed.  Returns ``(username, home_dir)``
    when multi-user mode is active, or ``(None, None)`` otherwise.
    """
    if not MULTI_USER:
        return None, None
    user_id = request.headers.get("x-user-id")
    if not user_id:
        return None, None
    return resolve_user(user_id)


app = FastAPI(
    title="Open Terminal",
    description="A remote terminal API.",
    version=_pkg_version("open-terminal"),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ALLOWED_ORIGINS.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def normalize_null_query_params(request: Request, call_next):
    """Strip query parameters whose value is the literal string 'null'."""
    from urllib.parse import urlencode

    raw_params = request.query_params.multi_items()
    cleaned = [(k, v) for k, v in raw_params if v.lower() != "null"]
    if len(cleaned) != len(raw_params):
        request.scope["query_string"] = urlencode(cleaned).encode("utf-8")
    return await call_next(request)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ExecRequest(BaseModel):
    command: str = Field(
        ...,
        description="Shell command to execute. Supports chaining (&&, ||, ;), pipes (|), and redirections.",
        json_schema_extra={"examples": ["echo hello", "ls -la && whoami"]},
    )
    cwd: Optional[str] = Field(
        None,
        description="Working directory for the command. Defaults to the server's current directory if not set.",
    )
    env: Optional[dict[str, str]] = Field(
        None,
        description="Extra environment variables merged into the subprocess environment.",
    )


class InputRequest(BaseModel):
    input: str = Field(
        ...,
        description="Text to send to the process's stdin. Include newline characters as needed.",
    )


class WriteRequest(BaseModel):
    path: str = Field(
        ...,
        description="Absolute or relative path to write to. Parent directories are created automatically.",
    )
    content: str = Field(
        ...,
        description="Text content to write to the file.",
    )


class ReplacementChunk(BaseModel):
    target: str = Field(
        ...,
        description="Exact string to find. Must match precisely, including whitespace.",
    )
    replacement: str = Field(
        ...,
        description="Content to replace the target with.",
    )
    start_line: Optional[int] = Field(
        None,
        description="Narrow the search to lines at or after this (1-indexed).",
        ge=1,
    )
    end_line: Optional[int] = Field(
        None,
        description="Narrow the search to lines at or before this (1-indexed).",
        ge=1,
    )
    allow_multiple: bool = Field(
        False,
        description="If true, replaces all occurrences. If false, errors when multiple matches are found.",
    )


class MkdirRequest(BaseModel):
    path: str = Field(
        ...,
        description="Directory path to create. Parent directories are created automatically.",
    )


class MoveRequest(BaseModel):
    source: str = Field(
        ...,
        description="Path to the file or directory to move.",
    )
    destination: str = Field(
        ...,
        description="Destination path (new location).",
    )


class ReplaceRequest(BaseModel):
    path: str = Field(
        ...,
        description="Path to the file to modify.",
    )
    replacements: list[ReplacementChunk] = Field(
        ...,
        description="List of find-and-replace operations to apply sequentially.",
    )



# ---------------------------------------------------------------------------
# Background process management
# ---------------------------------------------------------------------------


@dataclass
class BackgroundProcess:
    id: str
    command: str
    runner: ProcessRunner
    status: str = "running"
    exit_code: Optional[int] = None
    log_task: Optional[asyncio.Task] = field(default=None, repr=False)
    finished_at: Optional[float] = field(default=None, repr=False)
    log_path: Optional[str] = field(default=None, repr=False)


_processes: dict[str, BackgroundProcess] = {}
_EXPIRY_SECONDS = 300  # auto-clean finished processes after 5 min


async def _log_process(background_process: BackgroundProcess):
    """Read process output and persist to a log file."""
    log_file = None
    try:
        if background_process.log_path:
            await aiofiles.os.makedirs(
                os.path.dirname(background_process.log_path), exist_ok=True
            )
            log_file = await aiofiles.open(background_process.log_path, "a", encoding="utf-8")
            await log_file.write(
                json.dumps(
                    {
                        "type": "start",
                        "command": background_process.command,
                        "pid": background_process.runner.pid,
                        "ts": time.time(),
                    }
                )
                + "\n"
            )
            await log_file.flush()
    except OSError:
        log_file = None

    try:
        await background_process.runner.read_output(log_file)
    finally:
        exit_code = await background_process.runner.wait()
        background_process.exit_code = exit_code
        background_process.status = "done"
        background_process.finished_at = time.time()
        background_process.runner.close()
        if log_file:
            await log_file.write(
                json.dumps(
                    {
                        "type": "end",
                        "exit_code": background_process.exit_code,
                        "ts": time.time(),
                    }
                )
                + "\n"
            )
            await log_file.close()


async def _read_log(
    log_path: Optional[str],
    offset: int = 0,
    tail: Optional[int] = None,
) -> tuple[list[dict], int, bool]:
    """Read output entries from a JSONL log file.

    Returns (entries, next_offset, truncated).
    """
    entries: list[dict] = []
    if not log_path or not await aiofiles.os.path.isfile(log_path):
        return entries, 0, False

    async with aiofiles.open(log_path, encoding="utf-8") as f:
        lines = await f.readlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") in ("stdout", "stderr", "output"):
            entries.append({"type": record["type"], "data": record["data"]})

    total = len(entries)
    entries = entries[offset:]

    truncated = False
    if tail is not None and len(entries) > tail:
        entries = entries[-tail:]
        truncated = True

    return entries, total, truncated


def _cleanup_expired():
    """Remove finished processes that have expired."""
    now = time.time()
    expired = [
        process_id
        for process_id, background_process in _processes.items()
        if background_process.finished_at
        and now - background_process.finished_at > _EXPIRY_SECONDS
    ]
    for process_id in expired:
        del _processes[process_id]


def _get_process(process_id: str) -> BackgroundProcess:
    _cleanup_expired()
    background_process = _processes.get(process_id)
    if not background_process:
        raise HTTPException(status_code=404, detail="Process not found")
    return background_process


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get(
    "/health",
    operation_id="health_check",
    summary="Health check",
    description="Returns service status. No authentication required.",
)
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Config (capability discovery)
# ---------------------------------------------------------------------------


@app.get(
    "/api/config",
    include_in_schema=False,
)
async def get_config():
    """Return server feature flags for client-side discovery."""
    return {
        "features": {
            "terminal": ENABLE_TERMINAL,
            "notebooks": ENABLE_NOTEBOOKS,
        },
    }


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@app.get(
    "/files/cwd",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def get_cwd(request: Request):
    run_as_user, user_home = get_user_context(request)
    return {"cwd": user_home if run_as_user else os.getcwd()}


@app.post(
    "/files/cwd",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def set_cwd(http_request: Request, request: MkdirRequest):
    run_as_user, _ = get_user_context(http_request)
    target = os.path.abspath(request.path)
    if run_as_user:
        # In multi-user mode, cwd is per-user; don't touch the global server cwd.
        return {"cwd": target}
    if not await aiofiles.os.path.isdir(target):
        raise HTTPException(status_code=404, detail="Directory not found")
    try:
        os.chdir(target)
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"cwd": target}


@app.get(
    "/files/list",
    operation_id="list_files",
    summary="List directory contents",
    description="Return a structured listing of files and directories at the given path.",
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "Directory not found."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def list_files(
    request: Request,
    directory: str = Query(".", description="Directory path to list."),
):
    run_as_user, user_home = get_user_context(request)
    target = os.path.abspath(directory)

    if run_as_user:
        entries = await sudo_list_dir(run_as_user, target)
        return {"dir": target, "entries": entries}

    if not await aiofiles.os.path.isdir(target):
        raise HTTPException(status_code=404, detail="Directory not found")

    def _list_sync():
        entries = []
        for name in sorted(os.listdir(target)):
            full_path = os.path.join(target, name)
            try:
                file_stat = os.stat(full_path)
                entries.append(
                    {
                        "name": name,
                        "type": "directory" if os.path.isdir(full_path) else "file",
                        "size": file_stat.st_size,
                        "modified": file_stat.st_mtime,
                    }
                )
            except OSError:
                continue
        return entries

    entries = await asyncio.to_thread(_list_sync)
    return {"dir": target, "entries": entries}


@app.get(
    "/files/read",
    operation_id="read_file",
    summary="Read a file",
    description="Return the contents of a file. Text files return JSON with a content string. Supported binary types (configurable, default: image/*) return the raw binary with the appropriate Content-Type. Unsupported binary types are rejected. Optionally specify a line range for text files. This returns file content to you but does not show anything to the user. Use display_file to let the user see a file.",
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "File not found."},
        415: {"description": "Unsupported binary file type."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def read_file(
    request: Request,
    path: str = Query(..., description="Path to the file to read."),
    start_line: Optional[int] = Query(
        None, description="First line to return (1-indexed, inclusive).", ge=1
    ),
    end_line: Optional[int] = Query(
        None, description="Last line to return (1-indexed, inclusive).", ge=1
    ),
):
    run_as_user, _ = get_user_context(request)
    target = os.path.abspath(path)
    if not run_as_user and not await aiofiles.os.path.isfile(target):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        if run_as_user:
            raw = await sudo_read_file(run_as_user, target)
            content = raw.decode("utf-8")
            lines = content.splitlines(keepends=True)
        else:
            async with aiofiles.open(target, "r", encoding="utf-8", errors="strict") as f:
                content = await f.read()
                lines = content.splitlines(keepends=True)
    except (UnicodeDecodeError, ValueError):
        import mimetypes

        size = (await aiofiles.os.stat(target)).st_size
        mime, _ = mimetypes.guess_type(target)
        mime = mime or "application/octet-stream"

        # Extract text from PDFs so LLMs can read the content
        if mime == "application/pdf":
            reader = await asyncio.to_thread(PdfReader, target)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            lines = text.splitlines(keepends=True)
            start = (start_line or 1) - 1
            end = end_line or len(lines)
            return {
                "path": target,
                "total_lines": len(lines),
                "content": "".join(lines[start:end]),
            }

        # Return raw binary for allowed mime type prefixes (e.g. image/*)
        if any(mime.startswith(prefix) for prefix in BINARY_FILE_MIME_PREFIXES):
            async with aiofiles.open(target, "rb") as f:
                raw = await f.read()
            return Response(content=raw, media_type=mime)

        # Other binary files: reject (LLMs can't interpret raw bytes)
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported binary file type: {mime} ({size} bytes)",
        )

    start = (start_line or 1) - 1
    end = end_line or len(lines)
    return {
        "path": target,
        "total_lines": len(lines),
        "content": "".join(lines[start:end]),
    }


@app.get(
    "/files/display",
    operation_id="display_file",
    summary="Display a file to the user",
    description="Open a file in the user's file viewer so they can see it. Use this when the user wants to view or look at a file. This does not return file content to you — use read_file if you need to read the content yourself.",
    dependencies=[Depends(verify_api_key)],
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def display_file(
    path: str = Query(..., description="Absolute path to the file to display."),
):
    """Signal that a file should be displayed to the user.

    This endpoint does not serve file content itself. It returns the resolved
    path and whether the file exists. The consuming client is responsible for
    intercepting this response and presenting the file in its own UI (e.g.
    opening a preview pane, launching a viewer, etc.).
    """
    target = os.path.abspath(path)
    exists = await aiofiles.os.path.isfile(target)
    return {"path": target, "exists": exists}


@app.get(
    "/files/view",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def view_file(
    path: str = Query(..., description="Path to the file to view."),
):
    """Return raw file bytes with the appropriate Content-Type.

    Unlike read_file (which is designed for LLM consumption and restricts
    binary types), this endpoint serves any file as-is for UI previewing.
    """
    target = os.path.abspath(path)
    if not await aiofiles.os.path.isfile(target):
        raise HTTPException(status_code=404, detail="File not found")

    import mimetypes

    mime, _ = mimetypes.guess_type(target)
    mime = mime or "application/octet-stream"

    async with aiofiles.open(target, "rb") as f:
        raw = await f.read()
    return Response(content=raw, media_type=mime)


@app.post(
    "/files/write",
    operation_id="write_file",
    summary="Write a file",
    description="Write text content to a file. Creates parent directories automatically. Overwrites if the file already exists.",
    dependencies=[Depends(verify_api_key)],
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def write_file(http_request: Request, request: WriteRequest):
    run_as_user, _ = get_user_context(http_request)
    target = os.path.abspath(request.path)
    try:
        if run_as_user:
            await sudo_write_file(run_as_user, target, request.content)
        else:
            await aiofiles.os.makedirs(os.path.dirname(target), exist_ok=True)
            async with aiofiles.open(target, "w", encoding="utf-8") as f:
                await f.write(request.content)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": target, "size": len(request.content.encode())}


@app.post(
    "/files/mkdir",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def mkdir(http_request: Request, request: MkdirRequest):
    run_as_user, _ = get_user_context(http_request)
    target = os.path.abspath(request.path)
    try:
        if run_as_user:
            await sudo_mkdir(run_as_user, target)
        else:
            await aiofiles.os.makedirs(target, exist_ok=True)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": target}


@app.delete(
    "/files/delete",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def delete_entry(
    http_request: Request,
    path: str = Query(..., description="Path to delete."),
):
    run_as_user, _ = get_user_context(http_request)
    target = os.path.abspath(path)
    if not await aiofiles.os.path.exists(target):
        raise HTTPException(status_code=404, detail="Path not found")

    is_dir = await aiofiles.os.path.isdir(target)
    try:
        if run_as_user:
            await sudo_rm(run_as_user, target)
        else:
            if is_dir:
                await asyncio.to_thread(shutil.rmtree, target)
            else:
                await aiofiles.os.remove(target)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": target, "type": "directory" if is_dir else "file"}


@app.post(
    "/files/move",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def move_entry(http_request: Request, request: MoveRequest):
    run_as_user, _ = get_user_context(http_request)
    source = os.path.abspath(request.source)
    destination = os.path.abspath(request.destination)

    if not await aiofiles.os.path.exists(source):
        raise HTTPException(status_code=404, detail="Source path not found")

    dest_parent = os.path.dirname(destination)
    if not await aiofiles.os.path.isdir(dest_parent):
        raise HTTPException(status_code=400, detail="Destination parent directory not found")

    if await aiofiles.os.path.exists(destination):
        raise HTTPException(status_code=409, detail="Destination already exists")

    try:
        if run_as_user:
            await sudo_mv(run_as_user, source, destination)
        else:
            await asyncio.to_thread(shutil.move, source, destination)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"source": source, "destination": destination}


@app.post(
    "/files/replace",
    operation_id="replace_file_content",
    summary="Replace content in a file",
    description="Find and replace exact strings in a file. Supports multiple replacements in one call with optional line range narrowing.",
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "File not found."},
        400: {"description": "Target string not found or ambiguous match."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def replace_file_content(request: ReplaceRequest):
    target = os.path.abspath(request.path)
    if not await aiofiles.os.path.isfile(target):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        async with aiofiles.open(target, "r", encoding="utf-8", errors="replace") as f:
            content = await f.read()
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))

    for chunk in request.replacements:
        if chunk.start_line or chunk.end_line:
            lines = content.splitlines(keepends=True)
            start = (chunk.start_line or 1) - 1
            end = chunk.end_line or len(lines)
            search_region = "".join(lines[start:end])
        else:
            search_region = content

        count = search_region.count(chunk.target)
        if count == 0:
            raise HTTPException(
                status_code=400,
                detail=f"Target string not found: {chunk.target[:100]!r}",
            )
        if count > 1 and not chunk.allow_multiple:
            raise HTTPException(
                status_code=400,
                detail=f"Found {count} occurrences of target string but allow_multiple is false",
            )

        if chunk.start_line or chunk.end_line:
            new_region = search_region.replace(chunk.target, chunk.replacement)
            lines[start:end] = [new_region]
            content = "".join(lines)
        else:
            content = content.replace(chunk.target, chunk.replacement)

    try:
        async with aiofiles.open(target, "w", encoding="utf-8") as f:
            await f.write(content)
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"path": target, "size": len(content.encode())}


@app.get(
    "/files/grep",
    operation_id="grep_search",
    summary="Search file contents",
    description="Search for a text pattern across files in a directory. Returns structured matches with file paths, line numbers, and matching lines. Skips binary files.",
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "Search path not found."},
        400: {"description": "Invalid regex pattern."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def grep_search(
    query: str = Query(..., description="Text or regex pattern to search for."),
    path: str = Query(".", description="Directory or file to search in."),
    regex: bool = Query(False, description="Treat query as a regex pattern."),
    case_insensitive: bool = Query(
        False, description="Perform case-insensitive matching."
    ),
    include: Optional[list[str]] = Query(
        None,
        description="Glob patterns to filter files (e.g. '*.py'). Files must match at least one pattern.",
    ),
    match_per_line: bool = Query(
        True,
        description="If true, return each matching line with line numbers. If false, return only the names of matching files.",
    ),
    max_results: int = Query(
        50, description="Maximum number of matches to return.", ge=1, le=500
    ),
):
    target = os.path.abspath(path)
    if not await aiofiles.os.path.exists(target):
        raise HTTPException(status_code=404, detail="Search path not found")

    flags = re.IGNORECASE if case_insensitive else 0
    if regex:
        try:
            pattern = re.compile(query, flags)
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"Invalid regex: {exc}")
    else:
        pattern = re.compile(re.escape(query), flags)

    def _search_sync():
        def _matches_include(filename: str) -> bool:
            if not include:
                return True
            return any(fnmatch.fnmatch(filename, glob) for glob in include)

        matches = []
        truncated = False

        def _search_file(file_path: str):
            nonlocal truncated
            if truncated:
                return
            try:
                with open(file_path, "r", encoding="utf-8", errors="strict") as f:
                    for line_number, line in enumerate(f, 1):
                        if pattern.search(line):
                            if match_per_line:
                                matches.append(
                                    {
                                        "file": file_path,
                                        "line": line_number,
                                        "content": line.rstrip("\n\r"),
                                    }
                                )
                                if len(matches) >= max_results:
                                    truncated = True
                                    return
                            else:
                                matches.append({"file": file_path})
                                if len(matches) >= max_results:
                                    truncated = True
                                return  # one match per file is enough
            except (UnicodeDecodeError, ValueError, OSError):
                pass  # skip binary or unreadable files

        if os.path.isfile(target):
            _search_file(target)
        else:
            for dirpath, _, filenames in os.walk(target):
                if truncated:
                    break
                for filename in sorted(filenames):
                    if not _matches_include(filename):
                        continue
                    _search_file(os.path.join(dirpath, filename))

        return matches, truncated

    matches, truncated = await asyncio.to_thread(_search_sync)
    return {
        "query": query,
        "path": target,
        "matches": matches,
        "truncated": truncated,
    }


@app.get(
    "/files/glob",
    operation_id="glob_search",
    summary="Search files by name",
    description="Search for files and subdirectories by name within a specified directory using glob patterns. Results will include the relative path, type, size, and modification time.",
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "Search directory not found."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def glob_search(
    pattern: str = Query(..., description="Glob pattern to search for (e.g. '*.py')."),
    path: str = Query(".", description="Directory to search within."),
    exclude: Optional[list[str]] = Query(
        None, description="Glob patterns to exclude from search results."
    ),
    type: Optional[str] = Query(
        "any",
        description="Type filter: 'file', 'directory', or 'any'.",
        pattern="^(file|directory|any)$",
    ),
    max_results: int = Query(
        50, description="Maximum number of matches to return.", ge=1, le=500
    ),
):
    target = os.path.abspath(path)
    if not await aiofiles.os.path.isdir(target):
        raise HTTPException(status_code=404, detail="Search directory not found")

    def _glob_sync():
        matches = []
        truncated = False

        for dirpath, dirnames, filenames in os.walk(target):
            if truncated:
                break

            entries = []
            if type in ("any", "directory"):
                entries.extend([(d, "directory") for d in dirnames])
            if type in ("any", "file"):
                entries.extend([(f, "file") for f in filenames])

            for name, entry_type in sorted(entries, key=lambda x: x[0]):
                if truncated:
                    break

                full_path = os.path.join(dirpath, name)
                rel_path = os.path.relpath(full_path, target)

                # Check inclusion pattern
                if not fnmatch.fnmatch(name, pattern) and not fnmatch.fnmatch(
                    rel_path, pattern
                ):
                    continue

                # Check exclusion patterns
                if exclude and any(
                    fnmatch.fnmatch(name, excl) or fnmatch.fnmatch(rel_path, excl)
                    for excl in exclude
                ):
                    continue

                try:
                    file_stat = os.stat(full_path)
                    matches.append(
                        {
                            "path": rel_path,
                            "type": entry_type,
                            "size": file_stat.st_size,
                            "modified": file_stat.st_mtime,
                        }
                    )

                    if len(matches) >= max_results:
                        truncated = True
                        break
                except OSError:
                    pass

        return matches, truncated

    matches, truncated = await asyncio.to_thread(_glob_sync)
    return {
        "pattern": pattern,
        "path": target,
        "matches": matches,
        "truncated": truncated,
    }




@app.post(
    "/files/upload",
    include_in_schema=False,
    operation_id="upload_file",
    summary="Upload a file",
    description="Save a file to the specified path. Provide a `url` to fetch remotely, or send the file directly via multipart form data.",
    dependencies=[Depends(verify_api_key)],
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def upload_file(
    directory: str = Query(..., description="Destination directory for the file."),
    url: Optional[str] = Query(
        None,
        description="URL to download the file from. If omitted, expects a multipart file upload.",
    ),
    file: Optional[UploadFile] = File(
        None, description="The file to upload (if no URL provided)."
    ),
):
    if url:
        import httpx
        from urllib.parse import urlparse

        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        content = response.content
        filename = os.path.basename(urlparse(url).path) or "download"
    elif file:
        content = await file.read()
        filename = file.filename or "upload"
    else:
        raise HTTPException(
            status_code=400, detail="Provide either 'url' or a file upload."
        )

    try:
        await aiofiles.os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, filename)
        async with aiofiles.open(path, "wb") as f:
            await f.write(content)
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": path, "size": len(content)}




# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


@app.get(
    "/execute",
    operation_id="list_processes",
    summary="List running commands",
    description="Returns a list of all tracked background processes, including running, done, and killed.",
    dependencies=[Depends(verify_api_key)],
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def list_processes():
    _cleanup_expired()
    return [
        {
            "id": background_process.id,
            "command": background_process.command,
            "status": background_process.status,
            "exit_code": background_process.exit_code,
            "log_path": background_process.log_path,
        }
        for background_process in _processes.values()
    ]


@app.post(
    "/execute",
    operation_id="run_command",
    summary="Execute a command",
    description=_EXECUTE_DESCRIPTION,
    dependencies=[Depends(verify_api_key)],
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def execute(
    http_request: Request,
    request: ExecRequest,
    wait: Optional[float] = Query(
        None,
        description="Seconds to wait for the command to finish before returning. If the command completes in time, output is included inline. Null to return immediately.",
        ge=0,
        le=300,
    ),
    tail: Optional[int] = Query(
        None,
        description="Return only the last N output entries. Useful to limit response size when only recent output matters.",
        ge=1,
    ),
):
    run_as_user, user_home = get_user_context(http_request)
    cwd = request.cwd or (user_home if run_as_user else None)

    subprocess_env = {**os.environ, **request.env} if request.env else None
    runner = await create_runner(
        request.command, cwd, subprocess_env, run_as_user=run_as_user
    )

    process_id = uuid.uuid4().hex[:12]
    log_path = os.path.join(LOG_DIR, "processes", f"{process_id}.jsonl")
    background_process = BackgroundProcess(
        id=process_id, command=request.command, runner=runner, log_path=log_path
    )
    background_process.log_task = asyncio.create_task(_log_process(background_process))
    _processes[process_id] = background_process

    if wait is None and EXECUTE_TIMEOUT:
        wait = EXECUTE_TIMEOUT
    if wait is not None:
        try:
            await asyncio.wait_for(
                asyncio.shield(background_process.log_task), timeout=wait
            )
        except asyncio.TimeoutError:
            pass

    output, next_offset, truncated = await _read_log(
        background_process.log_path, offset=0, tail=tail
    )

    return {
        "id": process_id,
        "command": request.command,
        "status": background_process.status,
        "exit_code": background_process.exit_code,
        "output": output,
        "truncated": truncated,
        "next_offset": next_offset,
        "log_path": background_process.log_path,
    }


@app.get(
    "/execute/{process_id}/status",
    operation_id="get_process_status",
    summary="Get command status and output",
    description="Returns new output since the last poll, process status, and exit code. Output is drained on read to keep memory bounded.",
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "Process not found."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def get_status(
    process_id: str,
    wait: Optional[float] = Query(
        None,
        description="Seconds to wait for the process to finish before returning. Returns early if the process exits. Null to return immediately.",
        ge=0,
        le=300,
    ),
    offset: int = Query(
        0,
        description="Number of output entries to skip. Use next_offset from the previous response to get only new output.",
        ge=0,
    ),
    tail: Optional[int] = Query(
        None,
        description="Return only the last N output entries. Useful to limit response size when only recent output matters.",
        ge=1,
    ),
):
    background_process = _get_process(process_id)

    if wait is None and EXECUTE_TIMEOUT:
        wait = EXECUTE_TIMEOUT
    if wait is not None and background_process.status == "running":
        try:
            await asyncio.wait_for(
                asyncio.shield(background_process.log_task), timeout=wait
            )
        except asyncio.TimeoutError:
            pass

    output, next_offset, truncated = await _read_log(
        background_process.log_path, offset=offset, tail=tail
    )

    return {
        "id": background_process.id,
        "command": background_process.command,
        "status": background_process.status,
        "exit_code": background_process.exit_code,
        "output": output,
        "truncated": truncated,
        "next_offset": next_offset,
        "log_path": background_process.log_path,
    }


@app.post(
    "/execute/{process_id}/input",
    operation_id="send_process_input",
    summary="Send input to a running command",
    description="Write text to the process's stdin. Include newline characters as needed.",
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "Process not found."},
        400: {"description": "Process has already exited or stdin is closed."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def send_input(process_id: str, body: InputRequest):
    background_process = _get_process(process_id)
    if background_process.status != "running":
        raise HTTPException(status_code=400, detail="Process has already exited")

    # Convert literal escape sequences (\n, \x03 for Ctrl-C, etc.) into real
    # characters — LLMs often emit these as literal strings.
    text = body.input.encode("raw_unicode_escape").decode("unicode_escape")

    try:
        background_process.runner.write_input(text.encode())
        if isinstance(background_process.runner, PipeRunner):
            await background_process.runner.drain_input()
    except (BrokenPipeError, ConnectionResetError, OSError):
        raise HTTPException(status_code=400, detail="Process stdin is closed")

    return {"status": "ok"}


@app.delete(
    "/execute/{process_id}",
    operation_id="kill_process",
    summary="Kill a running command",
    description="Terminate the process. Sends SIGTERM by default for graceful shutdown. Use force=true to send SIGKILL.",
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "Process not found."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def kill_process(
    process_id: str,
    force: bool = Query(False, description="Send SIGKILL instead of SIGTERM."),
):
    background_process = _get_process(process_id)
    if background_process.status == "running":
        background_process.runner.kill(force=force)
        exit_code = await background_process.runner.wait()
        background_process.runner.close()
        background_process.status = "killed"
        background_process.exit_code = exit_code
    del _processes[process_id]
    return {"status": "killed"}


# ---------------------------------------------------------------------------
# Port detection & proxy
# ---------------------------------------------------------------------------

from open_terminal.utils.port import detect_listening_ports, get_descendant_pids

@app.get(
    "/ports",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def list_ports():
    """Return TCP ports currently listening on localhost.

    Only includes ports owned by descendant processes of open-terminal
    (i.e. things started via the terminal or /execute).
    """
    all_ports = await asyncio.to_thread(detect_listening_ports)

    own_pid = os.getpid()
    descendant_pids = await asyncio.to_thread(get_descendant_pids, own_pid)
    all_ports = [p for p in all_ports if p.get("pid") in descendant_pids]

    return {"ports": all_ports}


# -- Port proxy client (reused across requests) --
_port_proxy_client = None


async def _get_port_proxy_client():
    global _port_proxy_client
    if _port_proxy_client is None:
        import httpx
        _port_proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=5.0),
            follow_redirects=False,
        )
    return _port_proxy_client


@app.api_route(
    "/proxy/{port}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def port_proxy(port: int, path: str, request: Request):
    """Reverse-proxy a request to localhost:{port}/{path}."""
    if port < 1 or port > 65535:
        raise HTTPException(status_code=422, detail="Port must be between 1 and 65535")

    target_url = f"http://localhost:{port}/{path}"
    if request.query_params:
        target_url += f"?{request.query_params}"

    # Forward headers, stripping hop-by-hop and host.
    headers = dict(request.headers)
    for h in ("host", "transfer-encoding", "connection", "authorization"):
        headers.pop(h, None)

    body = await request.body()

    import httpx

    client = await _get_port_proxy_client()
    try:
        upstream = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body or None,
        )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail=f"Connection refused: localhost:{port}",
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail=f"Timeout connecting to localhost:{port}",
        )

    response_headers = dict(upstream.headers)
    for h in ("transfer-encoding", "connection", "content-encoding", "content-length"):
        response_headers.pop(h, None)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )


# ---------------------------------------------------------------------------
# Interactive terminal sessions (resource-oriented API)
# ---------------------------------------------------------------------------

if ENABLE_TERMINAL:

    import uuid as _uuid
    from datetime import datetime as _datetime
    from fastapi.responses import JSONResponse

    try:
        import select as _select
    except ImportError:
        _select = None  # Not available on all platforms in all contexts

    # Determine terminal backend: prefer Unix PTY, then pywinpty, else None
    if _PTY_AVAILABLE:
        _TERMINAL_BACKEND = "pty"
    else:
        try:
            from winpty import PtyProcess as _WinPtyProcess

            _TERMINAL_BACKEND = "winpty"
        except ImportError:
            _TERMINAL_BACKEND = None

    # Active terminal sessions: {id: {...}}
    _terminal_sessions: dict[str, dict] = {}


    def _cleanup_session(session_id: str):
        """Clean up a terminal session's resources."""
        session = _terminal_sessions.pop(session_id, None)
        if session is None:
            return

        backend = session.get("backend")

        if backend == "pty":
            try:
                os.close(session["master_fd"])
            except OSError:
                pass
            process = session["process"]
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

        elif backend == "winpty":
            pty_proc = session["pty_process"]
            if pty_proc.isalive():
                pty_proc.terminate()


    @app.post("/api/terminals", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def create_terminal(request: Request):
        """Create a new terminal session and return its ID."""
        if _TERMINAL_BACKEND is None:
            return JSONResponse(
                {"error": "PTY not available on this platform (install pywinpty on Windows)"},
                status_code=503,
            )

        # Prune dead sessions before checking limit
        if _TERMINAL_BACKEND == "pty":
            dead = [sid for sid, s in _terminal_sessions.items() if s["process"].poll() is not None]
        else:
            dead = [sid for sid, s in _terminal_sessions.items() if not s["pty_process"].isalive()]
        for sid in dead:
            _cleanup_session(sid)

        if len(_terminal_sessions) >= MAX_TERMINAL_SESSIONS:
            return JSONResponse(
                {"error": f"Maximum number of terminal sessions ({MAX_TERMINAL_SESSIONS}) reached"},
                status_code=429,
            )

        session_id = str(_uuid.uuid4())[:8]

        if _TERMINAL_BACKEND == "pty":
            try:
                master_fd, slave_fd = pty.openpty()
            except OSError:
                return JSONResponse(
                    {"error": "Out of PTY devices — too many active terminals or processes"},
                    status_code=503,
                )

            try:
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

                run_as_user, user_home = get_user_context(request)
                if run_as_user:
                    shell_cmd = ["sudo", "-u", run_as_user, "--", "/bin/bash"]
                    cwd = None  # sudo handles the user context; parent can't chdir into 700 dirs
                else:
                    shell_cmd = [os.environ.get("SHELL", "/bin/sh")]
                    cwd = os.getcwd()

                spawn_env = os.environ.copy()
                spawn_env.setdefault("TERM", TERMINAL_TERM)
                process = subprocess.Popen(
                    shell_cmd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=cwd,
                    env=spawn_env,
                    start_new_session=True,
                )
            except Exception:
                os.close(slave_fd)
                os.close(master_fd)
                raise
            os.close(slave_fd)

            # Set non-blocking
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            _terminal_sessions[session_id] = {
                "backend": "pty",
                "master_fd": master_fd,
                "process": process,
                "created_at": _datetime.utcnow().isoformat() + "Z",
                "pid": process.pid,
            }

        else:  # winpty
            shell = os.environ.get("COMSPEC", "cmd.exe")
            spawn_env = os.environ.copy()
            spawn_env.setdefault("TERM", TERMINAL_TERM)
            pty_proc = _WinPtyProcess.spawn(
                [shell],
                cwd=os.getcwd(),
                env=spawn_env,
                dimensions=(24, 80),
            )
            _terminal_sessions[session_id] = {
                "backend": "winpty",
                "pty_process": pty_proc,
                "created_at": _datetime.utcnow().isoformat() + "Z",
                "pid": pty_proc.pid,
            }

        session = _terminal_sessions[session_id]
        return {
            "id": session_id,
            "created_at": session["created_at"],
            "pid": session["pid"],
        }


    def _session_is_alive(session: dict) -> bool:
        """Check if a terminal session's process is still running."""
        if session["backend"] == "pty":
            return session["process"].poll() is None
        else:
            return session["pty_process"].isalive()


    @app.get("/api/terminals", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def list_terminals(request: Request):
        """List active terminal sessions."""
        result = []
        to_remove = []
        for sid, session in _terminal_sessions.items():
            if not _session_is_alive(session):
                to_remove.append(sid)
                continue
            result.append({
                "id": sid,
                "created_at": session["created_at"],
                "pid": session["pid"],
            })
        for sid in to_remove:
            _cleanup_session(sid)
        return result


    @app.get("/api/terminals/{session_id}", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def get_terminal(session_id: str, request: Request):
        """Get info about a terminal session."""
        session = _terminal_sessions.get(session_id)
        if session is None:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        if not _session_is_alive(session):
            _cleanup_session(session_id)
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return {
            "id": session_id,
            "created_at": session["created_at"],
            "pid": session["pid"],
        }


    @app.delete("/api/terminals/{session_id}", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def delete_terminal(session_id: str, request: Request):
        """Kill and remove a terminal session."""
        if session_id not in _terminal_sessions:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        _cleanup_session(session_id)
        return {"status": "deleted"}


    @app.websocket("/api/terminals/{session_id}")
    async def ws_terminal(ws: WebSocket, session_id: str):
        """Attach to an existing terminal session via WebSocket.

        Authentication is via **first-message auth**: after connecting, the client
        must send a JSON text frame as its first message::

            {"type": "auth", "token": "<api_key>"}

        The server validates the token and closes the connection if invalid.
        After authentication, the client sends keystrokes as **binary** frames
        and receives PTY output as binary frames.

        To resize, send a **text** JSON frame::

            {"type": "resize", "cols": 120, "rows": 40}
        """
        session = _terminal_sessions.get(session_id)
        if session is None:
            await ws.close(code=4004, reason="Session not found")
            return

        if not _session_is_alive(session):
            _cleanup_session(session_id)
            await ws.close(code=4004, reason="Session has ended")
            return

        await ws.accept()

        # First-message authentication
        if API_KEY:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
                payload = json.loads(msg)
                if payload.get("type") != "auth" or payload.get("token") != API_KEY:
                    await ws.close(code=4001, reason="Invalid API key")
                    return
            except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
                await ws.close(code=4001, reason="Auth timeout or invalid payload")
                return

        backend = session["backend"]
        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        # --- Platform-specific read/write/resize helpers ---

        if backend == "pty":
            master_fd = session["master_fd"]
            process = session["process"]

            def _blocking_read():
                """Read from PTY using select() so we don't block forever."""
                while not stop_event.is_set():
                    try:
                        rlist, _, _ = _select.select([master_fd], [], [], 0.1)
                        if rlist:
                            return os.read(master_fd, 4096)
                    except (OSError, ValueError):
                        return b""
                return b""

            def _check_alive():
                return process.poll() is None

            def _write_data(data: bytes):
                os.write(master_fd, data)

            def _do_resize(rows: int, cols: int):
                fcntl.ioctl(
                    master_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )

        else:  # winpty
            pty_proc = session["pty_process"]

            def _blocking_read():
                """Read from WinPTY process."""
                try:
                    data = pty_proc.read(4096)
                    return data.encode(errors="replace") if data else b""
                except EOFError:
                    return b""
                except Exception:
                    return b""

            def _check_alive():
                return pty_proc.isalive()

            def _write_data(data: bytes):
                pty_proc.write(data.decode(errors="replace"))

            def _do_resize(rows: int, cols: int):
                pty_proc.setwinsize(rows, cols)

        # --- Reader / writer tasks ---

        async def _pty_reader():
            """Forward PTY output -> WebSocket."""
            try:
                while not stop_event.is_set():
                    data = await loop.run_in_executor(None, _blocking_read)
                    if not data:
                        if stop_event.is_set():
                            break
                        if not _check_alive():
                            break
                        continue
                    try:
                        await ws.send_bytes(data)
                    except Exception:
                        break
            finally:
                pass

        reader_task = asyncio.create_task(_pty_reader())

        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                elif "bytes" in msg and msg["bytes"]:
                    await loop.run_in_executor(None, _write_data, msg["bytes"])
                elif "text" in msg and msg["text"]:
                    try:
                        payload = json.loads(msg["text"])
                        if payload.get("type") == "resize":
                            cols = payload.get("cols", 80)
                            rows = payload.get("rows", 24)
                            _do_resize(rows, cols)
                    except (json.JSONDecodeError, KeyError):
                        pass
        except WebSocketDisconnect:
            pass
        finally:
            stop_event.set()
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass
            # Clean up session on disconnect
            _cleanup_session(session_id)


# ---------------------------------------------------------------------------
# Notebook execution (optional)
# ---------------------------------------------------------------------------

if ENABLE_NOTEBOOKS:
    from open_terminal.notebooks import create_notebooks_router

    app.include_router(create_notebooks_router(verify_api_key))

