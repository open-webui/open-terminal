import asyncio

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

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from pypdf import PdfReader

from open_terminal.env import API_KEY, BINARY_FILE_MIME_PREFIXES, CORS_ALLOWED_ORIGINS, LOG_DIR
from open_terminal.runner import PipeRunner, ProcessRunner, create_runner


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

bearer_scheme = HTTPBearer(auto_error=False)


async def verify_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    if not API_KEY:
        return
    if not credentials or credentials.credentials != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


app = FastAPI(
    title="Open Terminal",
    description="A remote terminal API.",
    version="0.4.0",
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
            log_file = await aiofiles.open(background_process.log_path, "a")
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

    async with aiofiles.open(log_path) as f:
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
# Files
# ---------------------------------------------------------------------------


@app.get(
    "/files/cwd",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def get_cwd():
    return {"cwd": os.getcwd()}


@app.post(
    "/files/cwd",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def set_cwd(request: MkdirRequest):
    target = os.path.abspath(request.path)
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
    directory: str = Query(".", description="Directory path to list."),
):
    target = os.path.abspath(directory)
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
    path: str = Query(..., description="Path to the file to read."),
    start_line: Optional[int] = Query(
        None, description="First line to return (1-indexed, inclusive).", ge=1
    ),
    end_line: Optional[int] = Query(
        None, description="Last line to return (1-indexed, inclusive).", ge=1
    ),
):
    target = os.path.abspath(path)
    if not await aiofiles.os.path.isfile(target):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        async with aiofiles.open(target, "r", errors="strict") as f:
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
async def write_file(request: WriteRequest):
    target = os.path.abspath(request.path)
    try:
        await aiofiles.os.makedirs(os.path.dirname(target), exist_ok=True)
        async with aiofiles.open(target, "w") as f:
            await f.write(request.content)
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": target, "size": len(request.content.encode())}


@app.post(
    "/files/mkdir",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def mkdir(request: MkdirRequest):
    target = os.path.abspath(request.path)
    try:
        await aiofiles.os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": target}


@app.delete(
    "/files/delete",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def delete_entry(
    path: str = Query(..., description="Path to delete."),
):
    target = os.path.abspath(path)
    if not await aiofiles.os.path.exists(target):
        raise HTTPException(status_code=404, detail="Path not found")

    is_dir = await aiofiles.os.path.isdir(target)
    try:
        if is_dir:
            await asyncio.to_thread(shutil.rmtree, target)
        else:
            await aiofiles.os.remove(target)
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": target, "type": "directory" if is_dir else "file"}


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
        async with aiofiles.open(target, "r", errors="replace") as f:
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
        async with aiofiles.open(target, "w") as f:
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
                with open(file_path, "r", errors="strict") as f:
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
    subprocess_env = {**os.environ, **request.env} if request.env else None
    runner = await create_runner(request.command, request.cwd, subprocess_env)

    process_id = uuid.uuid4().hex[:12]
    log_path = os.path.join(LOG_DIR, "processes", f"{process_id}.jsonl")
    background_process = BackgroundProcess(
        id=process_id, command=request.command, runner=runner, log_path=log_path
    )
    background_process.log_task = asyncio.create_task(_log_process(background_process))
    _processes[process_id] = background_process

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
