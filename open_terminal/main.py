import asyncio
import hmac
from importlib.metadata import version as _pkg_version
import fnmatch
import json
import subprocess

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

from fastapi import Depends, FastAPI, File, HTTPException, Path as PathParam, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from open_terminal.env import API_KEY, BINARY_FILE_MIME_PREFIXES, CORS_ALLOWED_ORIGINS, ENABLE_NOTEBOOKS, ENABLE_SYSTEM_PROMPT, ENABLE_TERMINAL, EXECUTE_DESCRIPTION, EXECUTE_TIMEOUT, LOG_DIR, MAX_TERMINAL_SESSIONS, MULTI_USER, OPEN_TERMINAL_INFO, PROCESS_LOG_RETENTION, SESSION_CWD_TTL, SYSTEM_PROMPT, TERMINAL_TERM
from open_terminal.utils.apply_patch import (
    PatchParseError,
    commit_staged_patch,
    parse_apply_patch_text,
    stage_apply_patch,
)
from open_terminal.utils.runner import PipeRunner, ProcessRunner, create_runner
from open_terminal.utils.fs import UserFS

if MULTI_USER:
    from open_terminal.utils.user_isolation import check_environment, resolve_user
    check_environment()

if not API_KEY:
    raise SystemExit(
        "\n\033[91m"
        "  OPEN_TERMINAL_API_KEY is required.\n"
        "  Set via environment variable or --api-key flag.\n"
        "\033[0m"
    )

try:
    import fcntl
    import pty
    import struct
    import termios

    _PTY_AVAILABLE = True
except ImportError:
    _PTY_AVAILABLE = False  # Windows


def get_system_info() -> str:
    """Gather runtime system metadata for the OpenAPI description."""
    shell = os.environ.get("SHELL", "/bin/sh")
    user_part = f" as user '{os.getenv('USER', 'unknown')}'" if not MULTI_USER else ""
    return (
        f"This system is running {platform.system()} {platform.release()} ({platform.machine()}) "
        f"on {socket.gethostname()}{user_part} with {shell}. "
        f"Python {sys.version.split()[0]} is available."
    )


_CLI_CONTRACT_COMMANDS = (
    "rg",
    "git",
    "jq",
    "python3",
    "node",
    "curl",
    "tar",
    "zip",
    "unzip",
    "find",
    "sed",
    "awk",
    "file",
    "patch",
    "diff",
)


def _probe_cli_version(command: str) -> dict:
    executable = shutil.which(command)
    if not executable:
        return {"available": False, "path": None, "version": None}

    version_args = [executable, "--version"]
    if command == "node":
        version_args = [executable, "--version"]

    try:
        completed = subprocess.run(
            version_args,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        if command == "awk":
            try:
                completed = subprocess.run(
                    [executable, "-W", "version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
            except Exception:
                return {"available": True, "path": executable, "version": None}
        else:
            return {"available": True, "path": executable, "version": None}

    output = (completed.stdout or completed.stderr or "").strip()
    first_line = output.splitlines()[0] if output else None
    return {"available": True, "path": executable, "version": first_line}


def get_system_prompt() -> str:
    """Build a default system prompt for LLM integration."""
    if SYSTEM_PROMPT:
        return SYSTEM_PROMPT

    shell = os.environ.get("SHELL", "/bin/sh")
    user_part = f" as user '{os.getenv('USER', 'unknown')}'" if not MULTI_USER else ""

    prompt = (
        f"You have access to a computer running {platform.system()} {platform.release()} ({platform.machine()}) "
        f'on host "{socket.gethostname()}"{user_part} with {shell}. '
        f"Python {sys.version.split()[0]} is available.\n\n"
        "Use your tools to directly interact with the system \u2014 run commands, read and write files, "
        "and search the filesystem. "
        "Prefer verifying the current state before making changes. "
        "When running commands, check the output to confirm success. "
        "If a command produces no output, that typically means it succeeded."
    )

    if OPEN_TERMINAL_INFO:
        prompt += f"\n\n{OPEN_TERMINAL_INFO}"

    return prompt


_EXECUTE_DESCRIPTION = (
    "Run a shell command as a tracked background process and return a process_id.\n\n"
    "Use when: you need the primary system primitive for filesystem search, git, "
    "package management, builds, tests, diagnostics, or CLI tools not exposed as "
    "dedicated tools. Use get_environment first when you need OS, PATH, shell, "
    "permission, or CLI availability details.\n"
    "Inputs: command is the shell command string; cwd optionally sets the working "
    "directory; env optionally adds environment variables; wait controls how long "
    "to wait for completion; tail limits returned output entries. Relative paths "
    "resolve against the session cwd, or the supplied cwd when provided. Omit or "
    "set wait=null to use server default behavior; set wait=0 to return immediately.\n"
    "Returns: JSON with id/process_id, command, status, exit_code, output entries, "
    "truncated, next_offset, and log_path. If the command is still running, poll "
    "get_process_status with the process_id and next_offset.\n"
    "Errors: 401 means authentication failed; 422 means request validation failed. "
    "Use send_process_input for interactive stdin and kill_process to terminate "
    "long-running commands."
)
if EXECUTE_DESCRIPTION:
    _EXECUTE_DESCRIPTION += "\n\n" + EXECUTE_DESCRIPTION

bearer_scheme = HTTPBearer(auto_error=False)


async def verify_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    if not API_KEY:
        return
    if not credentials or not hmac.compare_digest(credentials.credentials, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


def get_filesystem(request: Request) -> UserFS:
    """Build a :class:`UserFS` scoped to the requesting user.

    When multi-user mode is active and the ``X-User-Id`` header is present,
    returns a ``UserFS`` that routes all I/O through ``sudo -u``.
    Otherwise returns a plain ``UserFS`` using stdlib.
    """
    if not MULTI_USER:
        return UserFS()
    user_id = request.headers.get("x-user-id")
    if not user_id:
        return UserFS()
    username, home = resolve_user(user_id)
    return UserFS(username=username, home=home)


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


@app.exception_handler(PermissionError)
async def permission_error_handler(request: Request, exc: PermissionError):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


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
    overwrite: bool = Field(
        False,
        description="Defaults to false. If false, writing to an existing path returns a 409 conflict instead of replacing it.",
    )


class ApplyPatchRequest(BaseModel):
    patch: str = Field(
        ...,
        description=(
            "Patch text using the OpenAI apply_patch format. Must start with "
            "'*** Begin Patch', contain one or more hunks such as "
            "'*** Add File:', '*** Update File:', or '*** Delete File:', and end "
            "with '*** End Patch'."
        ),
        json_schema_extra={
            "examples": [
                "*** Begin Patch\n*** Update File: path/to/file.py\n@@\n-old line\n+new line\n*** End Patch"
            ]
        },
    )
    dry_run: bool = Field(
        False,
        description="If true, validate and report changes without writing to disk.",
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


class CliVersionInfo(BaseModel):
    available: bool = Field(..., description="Whether the executable was found on PATH.")
    path: Optional[str] = Field(None, description="Resolved executable path, or null when unavailable.")
    version: Optional[str] = Field(None, description="First line of version output, or null when unavailable.")


class EnvironmentOSInfo(BaseModel):
    system: str = Field(..., description="Operating system name, for example Linux, Darwin, or Windows.")
    release: str = Field(..., description="Operating system release.")
    version: str = Field(..., description="Operating system version string.")
    machine: str = Field(..., description="Machine architecture.")
    python: str = Field(..., description="Python runtime version used by Open Terminal.")


class EnvironmentPermissionsInfo(BaseModel):
    multi_user: bool = Field(..., description="Whether Open Terminal is running in multi-user isolation mode.")
    run_as_user: Optional[str] = Field(None, description="Provisioned OS user used for commands and file operations, if any.")
    api_key_required: bool = Field(..., description="Whether API key authentication is enabled.")
    path_boundary: str = Field(..., description="Effective file access boundary, such as own_home_only or server_process.")


class EnvironmentResponse(BaseModel):
    os: EnvironmentOSInfo = Field(..., description="Operating system and Python runtime metadata.")
    hostname: str = Field(..., description="Host name reported by the runtime.")
    user: str = Field(..., description="Effective user for this request.")
    home: str = Field(..., description="Default home directory for this request.")
    cwd: str = Field(..., description="Current session working directory.")
    shell: str = Field(..., description="Default shell path.")
    environment: dict[str, str] = Field(..., description="Selected environment variables such as PATH.")
    cli_versions: dict[str, CliVersionInfo] = Field(..., description="Availability and version probe for the stable CLI contract.")
    permissions: EnvironmentPermissionsInfo = Field(..., description="Authentication and path-boundary metadata.")
    info: Optional[str] = Field(None, description="Operator-provided environment info, if configured.")


class ReadFileResponse(BaseModel):
    path: str = Field(..., description="Resolved file path that was read.")
    total_lines: int = Field(..., description="Total number of lines in the text or extracted document.")
    content: str = Field(..., description="Returned text content for the requested line range.")


class DisplayFileResponse(BaseModel):
    path: str = Field(..., description="Resolved path that the client should display to the user.")
    exists: bool = Field(..., description="Whether the resolved path currently exists as a file.")


class WriteFileResponse(BaseModel):
    path: str = Field(..., description="Resolved file path that was written.")
    size: int = Field(..., description="Number of UTF-8 bytes written.")


class PatchConflictInfo(BaseModel):
    path: str = Field(..., description="Resolved path where the conflict occurred.")
    reason: str = Field(..., description="Reason the patch could not be applied.")


class ErrorResponse(BaseModel):
    detail: str = Field(..., description="Human-readable error detail.")


class ApplyPatchConflictDetail(BaseModel):
    message: str = Field(..., description="Conflict summary.")
    conflicts: list[PatchConflictInfo] = Field(..., description="Patch conflicts that prevented applying any changes.")


class ApplyPatchConflictResponse(BaseModel):
    detail: ApplyPatchConflictDetail = Field(..., description="Structured patch conflict detail.")


class PatchChangeInfo(BaseModel):
    type: str = Field(..., description="Change type: add, update, delete, or move/update.")
    path: str = Field(..., description="Resolved source path affected by the change.")
    move_path: Optional[str] = Field(None, description="Resolved destination path for move hunks.")
    size: Optional[int] = Field(None, description="UTF-8 byte size of the resulting file content, when applicable.")


class ApplyPatchResponse(BaseModel):
    applied: bool = Field(..., description="True when changes were written to disk; false for dry_run.")
    dry_run: bool = Field(..., description="Whether this request validated without writing.")
    changes: list[PatchChangeInfo] = Field(..., description="Staged or applied changes.")
    conflicts: list[PatchConflictInfo] = Field(..., description="Conflicts; empty for successful 200 responses.")


class ProcessSummaryResponse(BaseModel):
    id: str = Field(..., description="process_id used with get_process_status, send_process_input, and kill_process.")
    command: str = Field(..., description="Command string that was started.")
    status: str = Field(..., description="Process status: running, done, or killed.")
    exit_code: Optional[int] = Field(None, description="Process exit code when available.")
    log_path: Optional[str] = Field(None, description="JSONL log path for persisted command output.")


class ProcessOutputEntry(BaseModel):
    type: str = Field(..., description="Output stream type: stdout, stderr, or output for PTY-combined output.")
    data: str = Field(..., description="Output text chunk.")


class ProcessStatusResponse(ProcessSummaryResponse):
    output: list[ProcessOutputEntry] = Field(..., description="Output entries returned by this poll.")
    truncated: bool = Field(..., description="Whether returned output was truncated by tail/log limits.")
    next_offset: int = Field(..., description="Offset to pass to get_process_status to read only new output next time.")


class StatusResponse(BaseModel):
    status: str = Field(..., description="Operation status.")



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


# ---------------------------------------------------------------------------
# Per-session working directory tracking
# ---------------------------------------------------------------------------
# Maps session_id → (absolute_cwd_path, last_accessed_timestamp).
# Replaces the old os.chdir() approach which was process-global and unsafe
# with concurrent sessions.
_session_cwds: dict[str, tuple[str, float]] = {}



def _expire_session_cwds():
    """Remove session cwd entries that haven't been accessed within the TTL."""
    now = time.time()
    expired = [sid for sid, (_, ts) in _session_cwds.items() if now - ts > SESSION_CWD_TTL]
    for sid in expired:
        del _session_cwds[sid]


def _get_session_cwd(session_id: str | None, fs: "UserFS") -> str:
    """Return the tracked cwd for *session_id*, or ``fs.home`` as default."""
    _expire_session_cwds()
    if session_id and session_id in _session_cwds:
        cwd, _ = _session_cwds[session_id]
        _session_cwds[session_id] = (cwd, time.time())  # refresh TTL
        return cwd
    return fs.home


def _set_session_cwd(session_id: str | None, path: str):
    """Store a session's cwd.  No-op if *session_id* is ``None``."""
    if session_id:
        _session_cwds[session_id] = (path, time.time())


from open_terminal.utils.log import log_process, read_log




def _cleanup_expired():
    """Remove finished processes that have expired.

    Also deletes log files older than *LOG_RETENTION_SECONDS*.
    """
    now = time.time()
    expired = [
        process_id
        for process_id, background_process in _processes.items()
        if background_process.finished_at
        and now - background_process.finished_at > _EXPIRY_SECONDS
    ]
    for process_id in expired:
        bp = _processes.pop(process_id)
        # Delete the log file if it has exceeded the retention period.
        if (
            bp.log_path
            and bp.finished_at
            and now - bp.finished_at > PROCESS_LOG_RETENTION
        ):
            try:
                os.remove(bp.log_path)
            except OSError:
                pass


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
    include_in_schema=False,
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
            "system": ENABLE_SYSTEM_PROMPT,
        },
    }


@app.get(
    "/environment",
    operation_id="get_environment",
    summary="Get runtime environment",
    description=(
        "Inspect the runtime environment and stable CLI contract.\n\n"
        "Use when: starting a task, before choosing OS-specific commands, when a "
        "command depends on PATH or shell behavior, or when you need permission and "
        "sandbox boundaries.\n"
        "Inputs: none.\n"
        "Returns: JSON fields os, hostname, user, home, cwd, shell, environment, "
        "cli_versions, permissions, and info. Each cli_versions entry reports "
        "available, path, and version.\n"
        "Errors: 401 means authentication failed."
    ),
    response_model=EnvironmentResponse,
    dependencies=[Depends(verify_api_key)],
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def get_environment(
    http_request: Request,
    fs: UserFS = Depends(get_filesystem),
):
    session_id = http_request.headers.get("x-session-id")
    cwd = _get_session_cwd(session_id, fs) if session_id else fs.home
    user = fs.username or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"

    return {
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
        },
        "hostname": socket.gethostname(),
        "user": user,
        "home": fs.home,
        "cwd": cwd,
        "shell": os.environ.get("SHELL", "/bin/sh"),
        "environment": {
            "PATH": os.environ.get("PATH", ""),
        },
        "cli_versions": {
            command: _probe_cli_version(command)
            for command in _CLI_CONTRACT_COMMANDS
        },
        "permissions": {
            "multi_user": MULTI_USER,
            "run_as_user": fs.username,
            "api_key_required": bool(API_KEY),
            "path_boundary": "own_home_only" if fs.username else "server_process",
        },
        "info": OPEN_TERMINAL_INFO or None,
    }


if ENABLE_SYSTEM_PROMPT:

    @app.get(
        "/system",
        include_in_schema=False,
        dependencies=[Depends(verify_api_key)],
    )
    async def get_system():
        """Return a system prompt for LLM integration."""
        return {"prompt": get_system_prompt()}


if OPEN_TERMINAL_INFO:

    @app.get(
        "/info",
        include_in_schema=False,
        operation_id="get_info",
        summary="Get environment info",
        description="Return operator-provided information about this environment. Use this to understand the system you are working with.",
        dependencies=[Depends(verify_api_key)],
    )
    async def get_info():
        return {"info": OPEN_TERMINAL_INFO}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@app.get(
    "/files/cwd",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def get_cwd(
    http_request: Request,
    fs: UserFS = Depends(get_filesystem),
):
    session_id = http_request.headers.get("x-session-id")
    return {"cwd": _get_session_cwd(session_id, fs)}


@app.post(
    "/files/cwd",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def set_cwd(
    http_request: Request,
    request: MkdirRequest,
    fs: UserFS = Depends(get_filesystem),
):
    session_id = http_request.headers.get("x-session-id")
    target = fs.resolve_path(request.path)
    if not fs.username and not await fs.isdir(target):
        raise HTTPException(status_code=404, detail="Directory not found")
    _set_session_cwd(session_id, target)
    return {"cwd": target}


@app.get(
    "/files/list",
    include_in_schema=False,
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
    http_request: Request,
    directory: str = Query(".", description="Directory path to list."),
    fs: UserFS = Depends(get_filesystem),
):
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    target = fs.resolve_path(directory, cwd=session_cwd)
    if not await fs.isdir(target):
        raise HTTPException(status_code=404, detail="Directory not found")
    entries = await fs.listdir(target)
    return {"dir": target, "entries": entries}


@app.get(
    "/files/read",
    operation_id="read_file",
    summary="Read a file",
    description=(
        "Read file content for agent analysis.\n\n"
        "Use when: you need to inspect text, a line range, extracted document text, "
        "or a supported image. For large text files, request start_line and end_line "
        "or use run_command with rg/sed/head/tail to avoid excessive context.\n"
        "Inputs: path is absolute or relative to the session cwd; start_line and "
        "end_line are optional 1-indexed inclusive bounds for text/document content.\n"
        "Returns: text files and extracted documents return JSON with path, "
        "total_lines, and content. Supported images return raw HTTP binary data "
        "with the image MIME type, not base64. Use display_file to show a file to "
        "the user.\n"
        "Errors: 404 means file not found; 415 means unsupported binary content; "
        "401 means authentication failed."
    ),
    response_model=ReadFileResponse,
    dependencies=[Depends(verify_api_key)],
    responses={
        200: {
            "description": "Returns JSON for text/document content. Raw binary image data is returned for supported image MIME types.",
            "content": {
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
                "image/webp": {"schema": {"type": "string", "format": "binary"}},
                "image/gif": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        404: {"description": "File not found."},
        415: {"description": "Unsupported binary file type."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def read_file(
    http_request: Request,
    path: str = Query(..., description="Path to the file to read."),
    start_line: Optional[int] = Query(
        None, description="First line to return (1-indexed, inclusive). Defaults to the beginning of the file.", ge=1
    ),
    end_line: Optional[int] = Query(
        None, description="Last line to return (1-indexed, inclusive). Defaults to the end of the file.", ge=1
    ),
    fs: UserFS = Depends(get_filesystem),
):
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    target = fs.resolve_path(path, cwd=session_cwd)
    if not await fs.isfile(target):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        content = await fs.read_text(target)
        lines = content.splitlines(keepends=True)
    except (UnicodeDecodeError, ValueError):
        import mimetypes

        raw = await fs.read(target)
        mime, _ = mimetypes.guess_type(target)
        mime = mime or "application/octet-stream"

        # Try document text extraction (PDF, Office, OpenDocument, etc.)
        from open_terminal.utils.documents import EXTRACTORS

        for ext_mime, ext_suffix, extractor in EXTRACTORS:
            if (ext_mime and mime == ext_mime) or (
                ext_suffix and target.lower().endswith(ext_suffix)
            ):
                text = await asyncio.to_thread(extractor, target)
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
            return Response(content=raw, media_type=mime)

        # Other binary files: reject (LLMs can't interpret raw bytes)
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported binary file type: {mime} ({len(raw)} bytes)",
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
    description=(
        "Make a file visible to the user in the client preview/viewer.\n\n"
        "Use when: the user asks to view, preview, open, or inspect a generated "
        "artifact directly. This is a UI/preview signal, not a content-reading tool.\n"
        "Inputs: path is the absolute path to display.\n"
        "Returns: JSON with path and exists. This does not return file content to "
        "you; use read_file if you need to read the content yourself.\n"
        "Errors: 401 means authentication failed; 422 means the path parameter was "
        "missing or invalid."
    ),
    response_model=DisplayFileResponse,
    dependencies=[Depends(verify_api_key)],
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def display_file(
    http_request: Request,
    path: str = Query(..., description="Absolute path to the file to display."),
    fs: UserFS = Depends(get_filesystem),
):
    """Signal that a file should be displayed to the user.

    This endpoint does not serve file content itself. It returns the resolved
    path and whether the file exists. The consuming client is responsible for
    intercepting this response and presenting the file in its own UI (e.g.
    opening a preview pane, launching a viewer, etc.).
    """
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    target = fs.resolve_path(path, cwd=session_cwd)
    exists = await fs.isfile(target)
    return {"path": target, "exists": exists}


@app.get(
    "/files/view",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def view_file(
    path: str = Query(..., description="Path to the file to view."),
    fs: UserFS = Depends(get_filesystem),
):
    """Return raw file bytes with the appropriate Content-Type.

    Unlike read_file (which is designed for LLM consumption and restricts
    binary types), this endpoint serves any file as-is for UI previewing.
    """
    target = fs.resolve_path(path)
    if not await fs.isfile(target):
        raise HTTPException(status_code=404, detail="File not found")

    import mimetypes

    mime, _ = mimetypes.guess_type(target)
    mime = mime or "application/octet-stream"
    raw = await fs.read(target)
    return Response(content=raw, media_type=mime)


@app.get(
    "/files/serve/{path:path}",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def serve_file(path: str, fs: UserFS = Depends(get_filesystem)):
    """Path-based alias for view_file — enables relative URL resolution in iframes."""
    return await view_file(path=f"/{path}", fs=fs)


@app.post(
    "/files/write",
    operation_id="write_file",
    summary="Write a file",
    description=(
        "Write complete text content to a file.\n\n"
        "Use when: creating a new text file or intentionally replacing the whole "
        "file. Prefer apply_patch for localized edits to existing files.\n"
        "Inputs: path is absolute or relative to the session cwd; content is the "
        "complete UTF-8 text to write; overwrite defaults to false. Parent "
        "directories are created automatically.\n"
        "Returns: JSON with path and size in UTF-8 bytes.\n"
        "Errors: 409 means the file already exists and overwrite=false; retry with "
        "overwrite=true only when replacing the whole file is intended. 401 means "
        "authentication failed; 422 means request validation failed."
    ),
    response_model=WriteFileResponse,
    dependencies=[Depends(verify_api_key)],
    responses={
        409: {
            "model": ErrorResponse,
            "description": "File already exists and overwrite=false.",
        },
        401: {"description": "Invalid or missing API key."},
    },
)
async def write_file(http_request: Request, request: WriteRequest, fs: UserFS = Depends(get_filesystem)):
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    target = fs.resolve_path(request.path, cwd=session_cwd)
    if not request.overwrite and await fs.exists(target):
        raise HTTPException(
            status_code=409,
            detail=(
                "File already exists and overwrite=false. Retry with overwrite=true "
                "only when replacing the whole file is intended; otherwise use "
                "apply_patch for localized edits."
            ),
        )
    try:
        await fs.write(target, request.content)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": target, "size": len(request.content.encode())}


@app.post(
    "/files/mkdir",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def mkdir(request: MkdirRequest, fs: UserFS = Depends(get_filesystem)):
    target = fs.resolve_path(request.path)
    try:
        await fs.mkdir(target)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": target}


@app.delete(
    "/files/delete",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def delete_entry(
    path: str = Query(..., description="Path to delete."),
    fs: UserFS = Depends(get_filesystem),
):
    target = fs.resolve_path(path)
    if not await fs.exists(target):
        raise HTTPException(status_code=404, detail="Path not found")
    is_dir = await fs.isdir(target)
    try:
        await fs.remove(target)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": target, "type": "directory" if is_dir else "file"}


@app.post(
    "/files/move",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def move_entry(request: MoveRequest, fs: UserFS = Depends(get_filesystem)):
    source = fs.resolve_path(request.source)
    destination = fs.resolve_path(request.destination)

    if not await fs.exists(source):
        raise HTTPException(status_code=404, detail="Source path not found")

    dest_parent = os.path.dirname(destination)
    if not await fs.isdir(dest_parent):
        raise HTTPException(status_code=400, detail="Destination parent directory not found")

    if await fs.exists(destination):
        raise HTTPException(status_code=409, detail="Destination already exists")

    try:
        await fs.move(source, destination)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"source": source, "destination": destination}


@app.post(
    "/files/replace",
    include_in_schema=False,
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
async def replace_file_content(http_request: Request, request: ReplaceRequest, fs: UserFS = Depends(get_filesystem)):
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    target = fs.resolve_path(request.path, cwd=session_cwd)
    if not await fs.isfile(target):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        content = await fs.read_text(target)
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
        await fs.write(target, content)
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"path": target, "size": len(content.encode())}


@app.post(
    "/files/apply_patch",
    operation_id="apply_patch",
    summary="Apply a patch",
    description=(
        "Apply an OpenAI apply_patch-format patch to one or more files.\n\n"
        "Use when: making localized file edits, adding files, deleting files, or "
        "moving files while preserving conflict detection. Use dry_run=true before "
        "risky or multi-file edits.\n"
        "Inputs: patch must start with '*** Begin Patch', contain hunks such as "
        "'*** Add File:', '*** Update File:', '*** Delete File:', or '*** Move to:', "
        "and end with '*** End Patch'. dry_run defaults to false. Example: "
        "'*** Begin Patch\\n*** Update File: path/to/file.py\\n@@\\n-old\\n+new\\n*** End Patch'.\n"
        "Returns: JSON with applied, dry_run, changes, and conflicts.\n"
        "Errors: 400 means patch syntax or commit failed; 409 means the patch did "
        "not apply cleanly. On 409, read_file the affected path, rebuild the patch "
        "from current content, and retry."
    ),
    response_model=ApplyPatchResponse,
    dependencies=[Depends(verify_api_key)],
    responses={
        400: {"description": "Patch syntax is invalid."},
        409: {
            "model": ApplyPatchConflictResponse,
            "description": "Patch could not be applied cleanly.",
        },
        401: {"description": "Invalid or missing API key."},
    },
)
async def apply_patch(
    http_request: Request,
    request: ApplyPatchRequest,
    fs: UserFS = Depends(get_filesystem),
):
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None

    try:
        changes = parse_apply_patch_text(request.patch)
    except PatchParseError as e:
        raise HTTPException(status_code=400, detail=str(e))

    staged, conflicts = await stage_apply_patch(changes, fs, session_cwd)
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "Patch could not be applied. Re-read the affected file with "
                    "read_file, rebuild the patch from current content, and retry."
                ),
                "conflicts": conflicts,
            },
        )

    if not request.dry_run:
        try:
            await commit_staged_patch(staged, fs)
        except (OSError, subprocess.CalledProcessError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    return {
        "applied": not request.dry_run,
        "dry_run": request.dry_run,
        "changes": [
            {
                "type": change.type,
                "path": change.path,
                "move_path": change.move_path,
                "size": (
                    len((change.new_content or "").encode())
                    if change.new_content is not None
                    else None
                ),
            }
            for change in staged
        ],
        "conflicts": [],
    }


@app.get(
    "/files/grep",
    include_in_schema=False,
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
    http_request: Request,
    query: str = Query(..., description="Text or regex pattern to search for."),
    path: str = Query(".", description="Directory or file to search in."),
    regex: bool = Query(True, description="Use regex. Set false for literal search."),
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
    fs: UserFS = Depends(get_filesystem),
):
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    target = fs.resolve_path(path, cwd=session_cwd)
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
            for dirpath, dirnames, filenames in os.walk(target):
                # Prune directories belonging to other users.
                dirnames[:] = [
                    d for d in dirnames
                    if fs.is_path_allowed(os.path.join(dirpath, d))
                ]
                if truncated:
                    break
                for filename in sorted(filenames):
                    if not _matches_include(filename):
                        continue
                    full = os.path.join(dirpath, filename)
                    if not fs.is_path_allowed(full):
                        continue
                    _search_file(full)

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
    include_in_schema=False,
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
    http_request: Request,
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
    fs: UserFS = Depends(get_filesystem),
):
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    target = fs.resolve_path(path, cwd=session_cwd)
    if not await aiofiles.os.path.isdir(target):
        raise HTTPException(status_code=404, detail="Search directory not found")

    def _glob_sync():
        matches = []
        truncated = False

        for dirpath, dirnames, filenames in os.walk(target):
            if truncated:
                break

            # Prune directories belonging to other users.
            dirnames[:] = [
                d for d in dirnames
                if fs.is_path_allowed(os.path.join(dirpath, d))
            ]

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
    description="Save a file to the specified path via multipart form data.",
    dependencies=[Depends(verify_api_key)],
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def upload_file(
    directory: str = Query(..., description="Destination directory for the file."),
    file: UploadFile = File(
        ..., description="The file to upload."
    ),
    fs: UserFS = Depends(get_filesystem),
):
    content = await file.read()
    filename = os.path.basename(file.filename or "upload")

    directory = fs.resolve_path(directory)
    path = os.path.normpath(os.path.join(directory, filename))

    try:
        await fs.mkdir(directory)
        await fs.write_bytes(path, content)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": path, "size": len(content)}


class ArchiveRequest(BaseModel):
    paths: list[str] = Field(
        ...,
        description="List of file or directory paths to include in the ZIP archive.",
    )


@app.post(
    "/files/archive",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def archive_paths(
    request: ArchiveRequest,
    fs: UserFS = Depends(get_filesystem),
):
    """Bundle files and/or directories into a single ZIP archive."""
    import io
    import zipfile

    if not request.paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    resolved = []
    for p in request.paths:
        target = fs.resolve_path(p)
        if not await fs.exists(target):
            raise HTTPException(status_code=404, detail=f"Path not found: {p}")
        resolved.append(target)

    # Derive a meaningful archive name from the input paths.
    if len(resolved) == 1:
        archive_name = os.path.basename(resolved[0].rstrip("/\\")) or "archive"
    else:
        archive_name = "download"

    def _build_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for target in resolved:
                if os.path.isfile(target):
                    zf.write(target, os.path.basename(target))
                elif os.path.isdir(target):
                    dirname = os.path.basename(target.rstrip("/\\")) or "dir"
                    for dirpath, dirnames, filenames in os.walk(target):
                        dirnames[:] = [
                            d for d in dirnames
                            if fs.is_path_allowed(os.path.join(dirpath, d))
                        ]
                        for fname in filenames:
                            full = os.path.join(dirpath, fname)
                            if not fs.is_path_allowed(full):
                                continue
                            arcname = os.path.join(
                                dirname, os.path.relpath(full, target)
                            )
                            zf.write(full, arcname)
        return buf.getvalue()

    data = await asyncio.to_thread(_build_zip)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{archive_name}.zip"',
        },
    )


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


@app.get(
    "/execute",
    operation_id="list_processes",
    summary="List tracked commands",
    description=(
        "List tracked commands started by run_command.\n\n"
        "Use when: you need to find active or recently finished process_ids before "
        "polling, sending input, or terminating a command.\n"
        "Inputs: none.\n"
        "Returns: JSON list of tracked commands with id, command, status, "
        "exit_code, and log_path. Status is running, done, or killed.\n"
        "Errors: 401 means authentication failed."
    ),
    response_model=list[ProcessSummaryResponse],
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
    response_model=ProcessStatusResponse,
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
        description="Seconds to wait for completion before returning. Omit or set null to use server default behavior; set 0 to return immediately.",
        ge=0,
        le=300,
    ),
    tail: Optional[int] = Query(
        None,
        description="Return only the last N output entries. Useful to limit response size when only recent output matters.",
        ge=1,
    ),
):
    fs = get_filesystem(http_request)
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    cwd = fs.resolve_path(request.cwd, cwd=session_cwd) if request.cwd else (session_cwd or fs.home)

    subprocess_env = {**os.environ, **request.env} if request.env else None
    runner = await create_runner(
        request.command, cwd, subprocess_env, run_as_user=fs.username
    )

    process_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    log_path = os.path.join(LOG_DIR, "processes", f"{process_id}.jsonl")
    background_process = BackgroundProcess(
        id=process_id, command=request.command, runner=runner, log_path=log_path
    )
    background_process.log_task = asyncio.create_task(log_process(background_process))
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

    output, next_offset, truncated = await read_log(
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
    description=(
        "Poll status and output for a process started by run_command.\n\n"
        "Use when: run_command returned while the command was still running, or you "
        "need more output from a tracked process.\n"
        "Inputs: process_id must be the id returned by run_command; offset should "
        "usually be the previous next_offset; wait controls how long to wait; tail "
        "limits returned output entries.\n"
        "Returns: JSON with id/process_id, command, status, exit_code, output, "
        "truncated, next_offset, and log_path. Use offset=next_offset to read only "
        "new output next time.\n"
        "Errors: 404 means the process_id is unknown or expired; 401 means "
        "authentication failed; 422 means request validation failed."
    ),
    response_model=ProcessStatusResponse,
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "Process not found."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def get_status(
    process_id: str = PathParam(
        ...,
        description="The process_id returned by run_command.",
    ),
    wait: Optional[float] = Query(
        None,
        description="Seconds to wait for the process to finish. Omit or set null to use server default behavior; set 0 to return immediately.",
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

    output, next_offset, truncated = await read_log(
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
    description=(
        "Write text to stdin for a running process started by run_command.\n\n"
        "Use when: an interactive command is waiting for input, confirmation, "
        "password text, Ctrl-C, or EOF.\n"
        "Inputs: process_id must be the id returned by run_command; input is text "
        "to send. Include newlines when the command expects Enter. Literal escape "
        "sequences such as \\n, \\x03, and \\x04 are converted before sending.\n"
        "Returns: JSON with status='ok' after input is accepted.\n"
        "Errors: 404 means the process_id is unknown or expired; 400 means the "
        "process exited or stdin is closed; 401 means authentication failed; 422 "
        "means request validation failed."
    ),
    response_model=StatusResponse,
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "Process not found."},
        400: {"description": "Process has already exited or stdin is closed."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def send_input(
    body: InputRequest,
    process_id: str = PathParam(
        ...,
        description="The process_id returned by run_command.",
    ),
):
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
    description=(
        "Terminate a process started by run_command.\n\n"
        "Use when: a tracked command is hung, no longer needed, or must be stopped "
        "before continuing.\n"
        "Inputs: process_id must be the id returned by run_command; force=false "
        "requests graceful termination, while force=true requests forceful "
        "termination.\n"
        "Returns: JSON with status='killed'.\n"
        "Errors: 404 means the process_id is unknown or expired; 401 means "
        "authentication failed; 422 means request validation failed. On Unix-like "
        "backends force=false sends SIGTERM and force=true sends SIGKILL; other "
        "platforms use the closest available behavior."
    ),
    response_model=StatusResponse,
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "Process not found."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def kill_process(
    process_id: str = PathParam(
        ...,
        description="The process_id returned by run_command.",
    ),
    force: bool = Query(False, description="Request forceful termination instead of graceful termination."),
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
async def list_ports(request: Request):
    """Return TCP ports currently listening on localhost.

    In multi-user mode, only shows ports owned by the requesting user.
    In single-user mode, shows ports owned by descendant processes.
    """
    all_ports = await asyncio.to_thread(detect_listening_ports)

    try:
        fs = get_filesystem(request)
    except Exception:
        # User provisioning failed (e.g. useradd rejected in restricted
        # container runtimes).  An unprovisioned user has no ports.
        return {"ports": []}

    if fs.username:
        # Filter by user UID
        import pwd
        try:
            user_uid = pwd.getpwnam(fs.username).pw_uid
            all_ports = [p for p in all_ports if p.get("uid") == user_uid]
        except KeyError:
            all_ports = []
    else:
        own_pid = os.getpid()
        descendant_pids = await asyncio.to_thread(get_descendant_pids, own_pid)
        all_ports = [p for p in all_ports if p.get("pid") in descendant_pids]

    # Strip uid from response (internal detail)
    for p in all_ports:
        p.pop("uid", None)

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
        """Clean up a terminal session's resources.

        For PTY sessions the shell is spawned with ``start_new_session=True``,
        giving it a dedicated process group.  We signal the *entire* group so
        that background jobs started inside the terminal (e.g. ``sleep 999 &``)
        are also reaped, and always call ``process.wait()`` to avoid zombies.
        """
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
                # Signal the whole process group first (graceful).
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # Forceful kill of the entire group.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    process.wait()

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

                fs = get_filesystem(request)

                # Use per-session cwd if available, else fall back to home
                session_id = request.headers.get("x-session-id", session_id)
                session_cwd = _get_session_cwd(session_id, fs) if session_id else None

                if fs.username:
                    shell_cmd = [
                        "script", "-qc",
                        f"sudo -i -u {fs.username}",
                        "/dev/null",
                    ]
                    cwd = session_cwd or fs.home
                else:
                    shell_cmd = [os.environ.get("SHELL", "/bin/sh")]
                    cwd = session_cwd or os.getcwd()

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
                if payload.get("type") != "auth" or not hmac.compare_digest(payload.get("token", ""), API_KEY):
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
    from open_terminal.utils.notebooks import create_notebooks_router

    app.include_router(create_notebooks_router(verify_api_key))
