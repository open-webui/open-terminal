import asyncio
from importlib.metadata import version as _pkg_version
import fnmatch
import heapq
import json
import logging
import tempfile
from contextlib import asynccontextmanager
import threading

import aiofiles
import aiofiles.os
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote as url_quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.formparsers import MultiPartParser, MultiPartException, parse_options_header
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from open_terminal.dependencies import get_filesystem, matches_api_key, verify_api_key
from open_terminal.env import API_KEY, BINARY_FILE_MIME_PREFIXES, CORS_ALLOWED_ORIGINS, ENABLE_NOTEBOOKS, ENABLE_SYSTEM_PROMPT, ENABLE_TERMINAL, EXECUTE_DESCRIPTION, EXECUTE_TIMEOUT, FILE_BROWSER_ROOT, LOG_DIR, MAX_TERMINAL_SESSIONS, MULTI_USER, OPEN_TERMINAL_INFO, PROCESS_LOG_RETENTION, SESSION_CWD_TTL, SYSTEM_PROMPT, TERMINAL_TERM
from open_terminal.env import sanitized_environ
from open_terminal.utils.runner import PipeRunner, ProcessRunner, create_runner, read_pty, write_pty, PtyRunner
from open_terminal.utils.fs import UserFS, FullFileResponse as FileResponse, TemporaryFileResponse, text_file_response, line_range_offsets
from open_terminal.utils.limits import (CHUNK_SIZE, NormalizeNullQueryMiddleware, run_io, run_worker)
from open_terminal.utils.file_compare import CompareRequest, run_comparison
from open_terminal.utils.search import is_regular_file, search_entries, matching_files, ranked_page
from open_terminal.utils.skills import join_skill_path, list_skill_resources, parse_skill_frontmatter

MATCH_PAGE_SIZE = 100
MAX_CONTENT_MATCHES_PER_FILE = 3
MAX_CONTENT_SEARCH_FILE_SIZE = 1 * 1024 * 1024
log = logging.getLogger(__name__)

if MULTI_USER:
    from open_terminal.utils.user_isolation import check_environment
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


def _system_prompt_variables() -> dict[str, str]:
    return {
        "os": platform.system(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "hostname": socket.gethostname(),
        "user": "" if MULTI_USER else os.getenv("USER", "unknown"),
        "shell": os.environ.get("SHELL", "/bin/sh"),
        "python_version": sys.version.split()[0],
        "home": os.path.expanduser("~"),
    }


def _expand_system_prompt_template(template: str) -> str:
    values = _system_prompt_variables()

    def replace(match: re.Match) -> str:
        key = match.group(1).strip()
        return values.get(key, match.group(0))

    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", replace, template)


def get_system_prompt() -> str:
    """Build a default system prompt for LLM integration."""
    if SYSTEM_PROMPT:
        return _expand_system_prompt_template(SYSTEM_PROMPT)

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
        "If a command produces no output, that typically means it succeeded.\n\n"
        "When delivering created or updated files to the user, call display_file for each deliverable "
        "before your final response. Do not invent download links or use sandbox: URLs. "
        "After displaying the files, briefly confirm completion without repeating links."
    )

    if OPEN_TERMINAL_INFO:
        prompt += f"\n\n{OPEN_TERMINAL_INFO}"

    return prompt


_EXECUTE_DESCRIPTION = (
    "Run a shell command in the background and return a command ID.\n\n"
    + get_system_info()
)
if EXECUTE_DESCRIPTION:
    _EXECUTE_DESCRIPTION += "\n\n" + EXECUTE_DESCRIPTION

def get_file_browser_root(fs: UserFS) -> dict[str, str] | None:
    configured = FILE_BROWSER_ROOT.strip()
    mode = configured.lower()
    if mode in ("", "home"):
        return {"path": fs.home, "label": "Home"}
    if mode == "filesystem":
        return None

    path = configured.replace("{{home}}", fs.home)
    if path == "~":
        path = fs.home
    elif path.startswith("~/"):
        path = os.path.join(fs.home, path[2:])

    root_path = fs.resolve_path(path)
    label = "Home" if root_path == fs.home else os.path.basename(root_path) or root_path
    return {"path": root_path, "label": label}


async def _preview_pdf(target: str) -> str | None:
    libreoffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not libreoffice:
        return None
    with tempfile.TemporaryDirectory() as tmpdir:
        await run_worker([
            libreoffice, "--headless", "--nologo", "--nofirststartwizard",
            "--nodefault", "--nolockcheck", "--convert-to", "pdf", "--outdir", tmpdir, target,
        ])
        pdf = os.path.join(tmpdir, os.path.splitext(os.path.basename(target))[0] + ".pdf")
        if not os.path.isfile(pdf):
            matches = sorted(name for name in os.listdir(tmpdir) if name.lower().endswith(".pdf"))
            if not matches:
                return None
            pdf = os.path.join(tmpdir, matches[0])
        fd, result = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            await run_io(shutil.copyfile, pdf, result)
            return result
        except BaseException:
            os.unlink(result)
            raise


@asynccontextmanager
async def lifespan(app):
    maintenance = asyncio.create_task(_maintenance_loop())
    try:
        yield
    finally:
        maintenance.cancel()
        await asyncio.gather(maintenance, return_exceptions=True)
        for process in list(_processes.values()):
            if process.finished_at is None:
                process.runner.kill(force=True)
        await asyncio.gather(*(p.log_task for p in _processes.values() if p.log_task), return_exceptions=True)
        if ENABLE_TERMINAL:
            for sid in list(_terminal_sessions):
                await _cleanup_session(sid)
        if ENABLE_NOTEBOOKS:
            from open_terminal.utils.notebooks import shutdown_notebooks
            await shutdown_notebooks()
        global _port_proxy_client
        if _port_proxy_client is not None:
            await _port_proxy_client.aclose()
            _port_proxy_client = None


app = FastAPI(
    lifespan=lifespan,
    title="Open Terminal",
    description="A remote terminal API.",
    version=_pkg_version("open-terminal"),
)
app.add_middleware(NormalizeNullQueryMiddleware)
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


class SkillSummary(BaseModel):
    id: str
    name: str
    description: str
    location: str
    scope: str
    source: str = "terminal"


class SkillReadResponse(SkillSummary):
    content: str
    resources: list[str]


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
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    finished_at: Optional[float] = field(default=None, repr=False)
    log_path: Optional[str] = field(default=None, repr=False)
    user_id: str = ""
    session_id: str = ""


_processes: dict[str, BackgroundProcess] = {}
_EXPIRY_SECONDS = 300  # auto-clean finished processes after 5 min


# ---------------------------------------------------------------------------
# Per-session working directory tracking
# ---------------------------------------------------------------------------
# Maps (user home, session_id) → (absolute_cwd_path, last_accessed_timestamp).
# Replaces the old os.chdir() approach which was process-global and unsafe
# with concurrent sessions.
_session_cwds: dict[tuple[str, str], tuple[str, float]] = {}



def _expire_session_cwds():
    """Remove session cwd entries that haven't been accessed within the TTL."""
    now = time.time()
    expired = [sid for sid, (_, ts) in _session_cwds.items() if now - ts > SESSION_CWD_TTL]
    for sid in expired:
        del _session_cwds[sid]


def _get_session_cwd(session_id: str | None, fs: "UserFS") -> str:
    """Return the tracked cwd for *session_id*, or ``fs.home`` as default."""
    _expire_session_cwds()
    key = (fs.home, session_id)
    if session_id and key in _session_cwds:
        cwd, _ = _session_cwds[key]
        _session_cwds[key] = (cwd, time.time())  # refresh TTL
        return cwd
    return fs.home


def _set_session_cwd(session_id: str | None, path: str, fs: "UserFS"):
    """Store a session's cwd.  No-op if *session_id* is ``None``."""
    if session_id:
        _expire_session_cwds()
        _session_cwds[(fs.home, session_id)] = (path, time.time())


from open_terminal.utils.log import log_process, log_response




def _cleanup_expired():
    now = time.time()
    finished = [p for p in _processes.values() if p.finished_at is not None]
    for process in finished:
        if now - process.finished_at > _EXPIRY_SECONDS:
            _processes.pop(process.id, None)


def _cleanup_log_files(active_paths):
    directory = os.path.join(LOG_DIR, "processes")
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.path in active_paths or not entry.name.endswith((".jsonl", ".rotating")):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False) and time.time() - entry.stat().st_mtime > PROCESS_LOG_RETENTION:
                        os.unlink(entry.path)
                except OSError:
                    pass
    except FileNotFoundError:
        pass


async def _maintenance_loop():
    while True:
        _cleanup_expired()
        _expire_session_cwds()
        if ENABLE_TERMINAL:
            for sid, session in list(_terminal_sessions.items()):
                if not _session_is_alive(session):
                    await _cleanup_session(sid)
        active = {p.log_path for p in _processes.values() if p.finished_at is None}
        try:
            await run_io(_cleanup_log_files, active)
        except OSError as exc:
            log.warning("Log retention cleanup failed: %s", exc)
        await asyncio.sleep(60)


def _get_process(process_id: str, request: Request) -> BackgroundProcess:
    _cleanup_expired()
    background_process = _processes.get(process_id)
    if not background_process or (
        background_process.user_id != request.headers.get("x-user-id", "")
        or background_process.session_id != request.headers.get("x-session-id", "")
    ):
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
        operation_id="get_info",
        summary="Get environment info",
        description="Return operator-provided information about this environment. Use this to understand the system you are working with.",
        dependencies=[Depends(verify_api_key)],
    )
    async def get_info():
        return {"info": OPEN_TERMINAL_INFO}


GLOBAL_SKILL_DIRS = (".agents/skills", ".cptr/skills", ".claude/skills", ".codex/skills")


async def _list_skills_in_dir(fs: UserFS, root: str, scope: str) -> list[SkillSummary]:
    if not await fs.isdir(root):
        return []

    skills: list[SkillSummary] = []
    for entry in await fs.listdir(root):
        dirname = str(entry.get("name") or "")
        if entry.get("type") != "directory" or not dirname or dirname.startswith("."):
            continue
        skill_dir = join_skill_path(root, dirname)
        skill_path = join_skill_path(skill_dir, "SKILL.md")
        if not await fs.isfile(skill_path):
            continue
        frontmatter, _ = parse_skill_frontmatter(await fs.read_text(skill_path))
        description = (frontmatter.get("description") or "").strip()
        if not description:
            continue
        name = (frontmatter.get("name") or dirname).strip()
        if not name:
            continue
        skills.append(
            SkillSummary(
                id=f"terminal:{url_quote(name, safe='')}",
                name=name,
                description=description[:1024],
                location=skill_path,
                scope=scope,
            )
        )
    return skills


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
    response = {
        "cwd": _get_session_cwd(session_id, fs),
        "home": fs.home,
    }
    root = get_file_browser_root(fs)
    if root:
        response["root"] = root
    return response


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
    if not await fs.isdir(target):
        raise HTTPException(status_code=404, detail="Directory not found")
    _set_session_cwd(session_id, target, fs)
    return {"cwd": target}


@app.get(
    "/skills",
    response_model=list[SkillSummary],
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def list_skills(
    http_request: Request,
    fs: UserFS = Depends(get_filesystem),
):
    roots = [("global", join_skill_path(fs.home, path)) for path in GLOBAL_SKILL_DIRS]
    seen_names: set[str] = set()
    seen_roots: set[str] = set()
    skills: list[SkillSummary] = []

    for scope, root in roots:
        root = os.path.normpath(root)
        if root in seen_roots:
            continue
        seen_roots.add(root)
        for skill in await _list_skills_in_dir(fs, root, scope):
            if skill.name in seen_names:
                continue
            seen_names.add(skill.name)
            skills.append(skill)

    return skills


@app.get(
    "/skills/read",
    response_model=SkillReadResponse,
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def read_skill(
    http_request: Request,
    name: str = Query(..., description="Skill name from /skills."),
    fs: UserFS = Depends(get_filesystem),
):
    for skill in await list_skills(http_request, fs):
        if skill.name != name:
            continue

        text = await fs.read_text(skill.location)
        _frontmatter, body = parse_skill_frontmatter(text)
        resources = await list_skill_resources(fs, os.path.dirname(skill.location))
        return SkillReadResponse(
            **skill.model_dump(),
            resources=resources,
            content=body,
        )

    raise HTTPException(status_code=404, detail="Skill not found")


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
    return {"dir": target, "writable": await fs.is_writable(target), "entries": entries}


@app.post("/files/compare", dependencies=[Depends(verify_api_key)], include_in_schema=False)
async def compare_files(http_request: Request, payload: CompareRequest, fs: UserFS = Depends(get_filesystem)):
    session_id = http_request.headers.get("x-session-id")
    cwd = _get_session_cwd(session_id, fs) if session_id else None
    return await run_comparison(http_request, payload, fs, cwd)


@app.get(
    "/files/read",
    operation_id="read_file",
    summary="Read a file",
    description="Read a file and return its contents. Supports text files and images (PNG, JPEG, WebP, etc.). For text files you can optionally request a specific line range. Images are returned as binary so you can view and analyze them directly. Use display_file to show a file to the user.",
    dependencies=[Depends(verify_api_key)],
    responses={
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

    fs._check_path(target)
    try:
        return await text_file_response(target, target, start_line, end_line)
    except UnicodeDecodeError:
        pass

    import mimetypes
    from open_terminal.utils.documents import EXTRACTORS

    mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
    if any(kind and mime == kind or suffix and target.lower().endswith(suffix) for kind, suffix, _ in EXTRACTORS):
        fd, extracted_path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            result = json.loads(await run_worker(
                [sys.executable, "-m", "open_terminal.utils.documents"],
                json.dumps({"path": target, "output": extracted_path}).encode(),
                request=http_request, timeout=None,
            ))
            if "error" in result:
                if result.get("error_type") == "PermissionError":
                    raise PermissionError(result["error"])
                raise ValueError(result["error"])
            return await text_file_response(extracted_path, target, start_line, end_line, False)
        finally:
            os.unlink(extracted_path)
    if any(mime.startswith(prefix) for prefix in BINARY_FILE_MIME_PREFIXES):
        return FileResponse(target, media_type=mime)
    raise HTTPException(415, f"Unsupported binary file type: {mime} ({os.path.getsize(target)} bytes)")


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
    preview: bool = Query(False, description="Return a best-effort rendered preview when available."),
    fs: UserFS = Depends(get_filesystem),
):
    """Return raw file bytes with the appropriate Content-Type.

    Unlike read_file (which is designed for LLM consumption and restricts
    binary types), this endpoint serves any file as-is for UI previewing.
    """
    target = fs.resolve_path(path)
    if not await fs.isfile(target):
        raise HTTPException(status_code=404, detail="File not found")

    if preview:
        ext = os.path.splitext(target)[1].lower()
        if ext in (".docx", ".pptx"):
            try:
                fs._check_path(target)
                pdf = await _preview_pdf(target)
            except Exception:
                log.exception("Rendered preview failed for %s", target)
                pdf = None

            if pdf:
                return TemporaryFileResponse(pdf, media_type="application/pdf")

    import mimetypes

    mime, _ = mimetypes.guess_type(target)
    mime = mime or "application/octet-stream"
    fs._check_path(target)
    return FileResponse(target, media_type=mime)


@app.get(
    "/files/serve/{path:path}",
    include_in_schema=False,
    dependencies=[Depends(verify_api_key)],
)
async def serve_file(path: str, fs: UserFS = Depends(get_filesystem)):
    """Path-based alias for view_file — enables relative URL resolution in iframes."""
    return await view_file(path=f"/{path}", preview=False, fs=fs)


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
async def write_file(http_request: Request, request: WriteRequest, fs: UserFS = Depends(get_filesystem)):
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    target = fs.resolve_path(request.path, cwd=session_cwd)
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
            start, end = line_range_offsets(content, chunk.start_line, chunk.end_line)
            search_region = content[start:end]
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
            content = content[:start] + new_region + content[max(start, end):]
        else:
            content = content.replace(chunk.target, chunk.replacement)

    try:
        await fs.write(target, content)
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

    matches, truncated = await run_io(_search_sync)
    return {
        "query": query,
        "path": target,
        "matches": matches,
        "truncated": truncated,
    }


@app.get(
    "/files/matches",
    operation_id="match_files",
    summary="Search files by name and contents",
    description="Search files and directories by ranked name, path, and content matches.",
    dependencies=[Depends(verify_api_key)],
    responses={
        400: {"description": "Blank query."},
        404: {"description": "Search directory not found."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def match_files(
    http_request: Request,
    query: str = Query(..., description="Literal text to match."),
    path: str = Query(".", description="Directory to search within."),
    show_hidden: bool = Query(False, description="Include hidden dotfiles and dot-directories."),
    offset: int = Query(0, description="Result offset.", ge=0),
    limit: int = Query(
        MATCH_PAGE_SIZE,
        description="Maximum number of results to return.",
        ge=1,
        le=MATCH_PAGE_SIZE,
    ),
    fs: UserFS = Depends(get_filesystem),
):
    query = query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query must not be blank")

    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    target = fs.resolve_path(path, cwd=session_cwd)
    if not fs.is_path_allowed(target) or not await aiofiles.os.path.isdir(target):
        raise HTTPException(status_code=404, detail="Search directory not found")

    def _matches_sync():
        query_lower = query.lower()

        def content_lines(path):
            with open(path, encoding="utf-8", errors="replace", newline="\n" if rg_matches is not None else None) as source:
                if rg_matches is not None:
                    yield from enumerate(source, 1)
                    return
                number = 0
                for line in source:
                    for text in line.splitlines():
                        number += 1
                        yield number, text

        def candidates():
            for entry_path, entry_type in search_entries(fs, target, show_hidden):
                rel_path = os.path.relpath(entry_path, target).replace("\\", "/")
                name = os.path.basename(entry_path)
                name_lower, rel_lower = name.lower(), rel_path.lower()
                score = (0 if name_lower == query_lower else 1 if name_lower.startswith(query_lower)
                    else 2 if query_lower in name_lower else 3 if query_lower in rel_lower else 4)
                content_matches = []
                if entry_type == "file" and not os.path.islink(entry_path) and is_regular_file(entry_path):
                    try:
                        size = os.path.getsize(entry_path)
                        eligible = rg_matches(entry_path) if rg_matches is not None else size <= MAX_CONTENT_SEARCH_FILE_SIZE
                        if eligible:
                            with open(entry_path, "rb") as source:
                                binary = rg_matches is None and b"\0" in source.read(8192)
                            for number, text in (() if binary else content_lines(entry_path)):
                                index = text.lower().find(query_lower)
                                if index >= 0:
                                    content_matches.append({"line": number, "column": len(text[:index].encode("utf-16-le")) // 2 + 1, "text": text.rstrip("\r\n")})
                                    if len(content_matches) >= MAX_CONTENT_MATCHES_PER_FILE:
                                        break
                    except (OSError, UnicodeError):
                        pass
                if score == 4 and not content_matches:
                    continue
                yield (score, len(rel_path), rel_lower, {
                    "path": entry_path, "relative_path": rel_path, "name": name,
                    "type": entry_type, "name_match": score < 4, "content_matches": content_matches,
                })

        with matching_files(query, target, show_hidden) as rg_matches:
            return ranked_page(candidates(), offset, limit)

    return await run_io(_matches_sync)


@app.get(
    "/files/search",
    operation_id="search_files",
    summary="Search files by name",
    description="Search for files and subdirectories by ranked filename match.",
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "Search directory not found."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def search_files(
    http_request: Request,
    query: str = Query("", description="Filename search term."),
    path: str = Query(".", description="Directory to search within."),
    limit: int = Query(20, description="Maximum number of matches to return.", ge=1, le=100),
    type: Optional[str] = Query(
        "any",
        description="Type filter: 'file', 'directory', or 'any'.",
        pattern="^(file|directory|any)$",
    ),
    show_hidden: bool = Query(False, description="Include hidden dotfiles and dot-directories."),
    fs: UserFS = Depends(get_filesystem),
):
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    target = fs.resolve_path(path, cwd=session_cwd)
    if not fs.is_path_allowed(target) or not await aiofiles.os.path.isdir(target):
        raise HTTPException(status_code=404, detail="Search directory not found")

    def _search_sync():
        query_lower = query.strip().lower()
        def candidates():
            for full_path, entry_type in search_entries(fs, target, show_hidden, git_files_as_files=True):
                if type != "any" and entry_type != type:
                    continue
                name = os.path.basename(full_path)
                lower = name.lower()
                if query_lower and query_lower not in lower:
                    continue
                rank = 0 if lower == query_lower else 1 if lower.startswith(query_lower) else 2
                try:
                    info = os.stat(full_path)
                except OSError:
                    continue
                yield (rank, len(name), os.path.relpath(full_path, target).lower(), {
                    "path": full_path, "name": name, "type": entry_type, "size": info.st_size, "modified": info.st_mtime,
                })
        return [item[3] for item in heapq.nsmallest(limit, candidates(), key=lambda item: item[:3])]

    return {"results": await run_io(_search_sync)}


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

    matches, truncated = await run_io(_glob_sync)
    return {
        "pattern": pattern,
        "path": target,
        "matches": matches,
        "truncated": truncated,
    }




class UploadParser(MultiPartParser):
    async def parse(self):
        try:
            return await super().parse()
        except BaseException:
            # Starlette closes these on MultiPartException only. Disconnects,
            # cancellation and other read errors must release spools as well.
            for file in self._files_to_close_on_error:
                file.close()
            raise



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
    http_request: Request,
    directory: str = Query(..., description="Destination directory for the file."),
    fs: UserFS = Depends(get_filesystem),
):
    try:
        content_type, _ = parse_options_header(http_request.headers.get("content-type"))
        if content_type == b"multipart/form-data":
            form = await UploadParser(http_request.headers, http_request.stream()).parse()
        else:
            form = await http_request.form()
    except MultiPartException as exc:
        raise HTTPException(400, exc.message) from None
    file = form.get("file")
    try:
        if file is None or file == "":
            raise RequestValidationError([{"type": "missing", "loc": ("body", "file"), "msg": "Field required", "input": None}])
        file = TypeAdapter(UploadFile).validate_python(file)
    except ValidationError as exc:
        await form.close()
        raise RequestValidationError([{**error, "loc": ("body", "file", *error["loc"])} for error in exc.errors(include_url=False)]) from exc
    except BaseException:
        await form.close()
        raise
    filename = os.path.basename(file.filename or "upload")
    directory = fs.resolve_path(directory)
    path = os.path.normpath(os.path.join(directory, filename))
    size = 0
    try:
        fs._check_path(path)
        await fs.mkdir(directory)
        async with aiofiles.open(path, "wb") as output:
            while chunk := await file.read(CHUNK_SIZE):
                size += len(chunk)
                await output.write(chunk)
        await fs._chown(path)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except OSError as exc:
        raise HTTPException(400, str(exc))
    finally:
        await form.close()
    return {"path": path, "size": size}


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

    fd, temporary = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    stop = threading.Event()

    def archive_files(target):
        if os.path.isfile(target):
            yield target
        elif os.path.isdir(target):
            for directory, dirs, files in os.walk(target):
                dirs[:] = [name for name in dirs if fs.is_path_allowed(os.path.join(directory, name))]
                for name in files:
                    full = os.path.join(directory, name)
                    if fs.is_path_allowed(full):
                        yield full

    def _build_zip():
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for target in resolved:
                fs._check_path(target)
                for full in archive_files(target):
                    if stop.is_set():
                        return
                    arcname = os.path.basename(full) if full == target else os.path.join(os.path.basename(target.rstrip("/\\")) or "dir", os.path.relpath(full, target))
                    info = zipfile.ZipInfo.from_file(full, arcname)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with open(full, "rb") as source, archive.open(info, "w") as destination:
                        while chunk := source.read(CHUNK_SIZE):
                            if stop.is_set():
                                return
                            destination.write(chunk)

    task = asyncio.create_task(asyncio.to_thread(_build_zip))
    try:
        await asyncio.shield(task)
        return TemporaryFileResponse(temporary, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{archive_name}.zip"'})
    except BaseException:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
        os.unlink(temporary)
        raise


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


@app.get(
    "/execute",
    operation_id="list_processes",
    summary="List running commands",
    description="Returns tracked processes for the current user and chat, including running, done, and killed.",
    dependencies=[Depends(verify_api_key)],
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def list_processes(http_request: Request):
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
        if background_process.user_id == http_request.headers.get("x-user-id", "")
        and background_process.session_id == http_request.headers.get("x-session-id", "")
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
    _cleanup_expired()
    fs = get_filesystem(http_request)
    session_id = http_request.headers.get("x-session-id")
    session_cwd = _get_session_cwd(session_id, fs) if session_id else None
    cwd = fs.resolve_path(request.cwd, cwd=session_cwd) if request.cwd else (session_cwd or fs.home)

    runner = await create_runner(request.command, cwd, request.env, run_as_user=fs.username)

    process_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    log_path = os.path.join(LOG_DIR, "processes", f"{process_id}.jsonl")
    background_process = BackgroundProcess(
        id=process_id, command=request.command, runner=runner, log_path=log_path,
        user_id=http_request.headers.get("x-user-id", ""),
        session_id=http_request.headers.get("x-session-id", ""),
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

    return await log_response(background_process.log_path, {
        "id": process_id,
        "command": request.command,
        "cwd": cwd,
        "status": background_process.status,
        "exit_code": background_process.exit_code,
        "log_path": background_process.log_path,
    }, offset=0, tail=tail)


@app.get(
    "/execute/{process_id}/status",
    operation_id="get_process_status",
    summary="Get command status and output",
    description="Returns new output since the given offset, process status, and exit code. Reads do not consume output; each reader keeps its own offset.",
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "Process not found."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def get_status(
    process_id: str,
    http_request: Request,
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
    background_process = _get_process(process_id, http_request)

    if wait is None and EXECUTE_TIMEOUT:
        wait = EXECUTE_TIMEOUT
    if wait is not None and background_process.status == "running":
        try:
            await asyncio.wait_for(
                asyncio.shield(background_process.log_task), timeout=wait
            )
        except asyncio.TimeoutError:
            pass

    return await log_response(background_process.log_path, {
        "id": background_process.id,
        "command": background_process.command,
        "status": background_process.status,
        "exit_code": background_process.exit_code,
        "log_path": background_process.log_path,
    }, offset=offset, tail=tail)


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
async def send_input(process_id: str, body: InputRequest, http_request: Request):
    background_process = _get_process(process_id, http_request)
    if background_process.status != "running":
        raise HTTPException(status_code=400, detail="Process has already exited")

    # Convert literal escape sequences (\n, \x03 for Ctrl-C, etc.) into real
    # characters — LLMs often emit these as literal strings.
    text = body.input.encode("raw_unicode_escape").decode("unicode_escape")

    try:
        async with background_process.input_lock:
            if isinstance(background_process.runner, PtyRunner):
                await write_pty(background_process.runner._master_fd, text.encode())
            else:
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
    http_request: Request,
    force: bool = Query(False, description="Send SIGKILL instead of SIGTERM."),
):
    background_process = _get_process(process_id, http_request)
    if background_process.status == "running":
        background_process.status = "killed"
        background_process.runner.kill(force=force)
        # The log task owns draining, reaping and closing the runner.
        await asyncio.shield(background_process.log_task)
    background_process.finished_at = time.time()
    return {"status": "killed"}


# ---------------------------------------------------------------------------
# Port detection & proxy
# ---------------------------------------------------------------------------

from open_terminal.utils.port import detect_listening_ports, get_descendant_pids


async def _visible_ports(request: Request) -> list[dict]:
    all_ports = await run_io(detect_listening_ports)

    try:
        fs = get_filesystem(request)
    except Exception:
        # User provisioning failed (e.g. useradd rejected in restricted
        # container runtimes).  An unprovisioned user has no ports.
        return []

    if fs.username:
        # Filter by user UID
        import pwd
        try:
            user_uid = pwd.getpwnam(fs.username).pw_uid
            return [p for p in all_ports if p.get("uid") == user_uid]
        except KeyError:
            return []

    own_pid = os.getpid()
    descendant_pids = await run_io(get_descendant_pids, own_pid)
    return [p for p in all_ports if p.get("pid") in descendant_pids]


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
    all_ports = await _visible_ports(request)

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

    if not any(p.get("port") == port for p in await _visible_ports(request)):
        raise HTTPException(status_code=404, detail="Port not found")

    target_url = f"http://localhost:{port}/{path}"
    if request.query_params:
        target_url += f"?{request.query_params}"

    # Forward headers, stripping hop-by-hop and host.
    headers = dict(request.headers)
    for h in ("host", "transfer-encoding", "connection", "authorization"):
        headers.pop(h, None)

    import httpx

    client = await _get_port_proxy_client()
    try:
        upstream_request = client.build_request(
            method=request.method, url=target_url, headers=headers, content=request.stream(),
        )
        upstream = await client.send(upstream_request, stream=True)
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

    class ProxyResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                await upstream.aclose()

    return ProxyResponse(upstream.aiter_bytes(), status_code=upstream.status_code, headers=response_headers)


# ---------------------------------------------------------------------------
# Interactive terminal sessions (resource-oriented API)
# ---------------------------------------------------------------------------

if ENABLE_TERMINAL:

    import uuid as _uuid
    from datetime import datetime as _datetime
    from fastapi.responses import JSONResponse

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


    async def _cleanup_session(session_id: str):
        """Clean up a terminal session's resources.

        For PTY sessions the shell is spawned with ``start_new_session=True``,
        giving it a dedicated process group.  We signal the *entire* group so
        that background jobs started inside the terminal (e.g. ``sleep 999 &``)
        are also reaped, and always call ``process.wait()`` to avoid zombies.
        """
        session = _terminal_sessions.pop(session_id, None)
        if session is None:
            return

        tasks = session.get("io_tasks", ())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        backend = session.get("backend")

        if backend == "pty":
            try:
                os.close(session["master_fd"])
            except OSError:
                pass

            process = session["process"]
            # The leader may have exited while background children remain.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.to_thread(process.wait, timeout=3)
            except subprocess.TimeoutExpired:
                pass
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                await asyncio.to_thread(process.wait)

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
            await _cleanup_session(sid)

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
                chat_id = request.headers.get("x-session-id")
                session_cwd = _get_session_cwd(chat_id, fs) if chat_id else None

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

                spawn_env = sanitized_environ()
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
            spawn_env = sanitized_environ()
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
        session.update(
            user_id=request.headers.get("x-user-id", ""),
            chat_id=request.headers.get("x-session-id", ""),
            output=bytearray(),
            connected=False,
            input_lock=asyncio.Lock(),
        )
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


    def _owns_terminal(session: dict, request) -> bool:
        return (
            session["user_id"] == request.headers.get("x-user-id", "")
            and session["chat_id"] == request.headers.get("x-session-id", "")
        )


    def _user_terminal(request: Request) -> dict:
        for session in reversed(_terminal_sessions.values()):
            if _owns_terminal(session, request) and session["connected"] and _session_is_alive(session):
                return session
        raise HTTPException(status_code=404, detail="No connected user shell")


    async def _write_terminal(session: dict, data: bytes):
        async with session["input_lock"]:
            if session["backend"] == "pty":
                await write_pty(session["master_fd"], data)
            else:
                await asyncio.to_thread(session["pty_process"].write, data.decode(errors="replace"))


    @app.get("/api/terminals/user/output", operation_id="read_user_terminal", dependencies=[Depends(verify_api_key)])
    async def read_user_terminal(request: Request):
        """Read recent output from the user's connected shell. Inspect before sending input:
        a foreground program may already be running. Does not create a shell."""
        session = _user_terminal(request)
        return {"output": bytes(session["output"]).decode(errors="replace")}


    @app.post("/api/terminals/user/input", operation_id="send_user_terminal_input", dependencies=[Depends(verify_api_key)])
    async def send_user_terminal_input(request: Request, body: InputRequest):
        """Send literal text to the user's connected shell or its foreground program.
        Include actual newlines to submit input. Read the shell first. Sending input
        does not wait for command completion and does not create a shell."""
        session = _user_terminal(request)
        try:
            await _write_terminal(session, body.input.encode())
        except (OSError, EOFError):
            raise HTTPException(status_code=409, detail="User shell has disconnected")
        return {"status": "ok"}


    @app.get("/api/terminals", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def list_terminals(request: Request):
        """List active terminal sessions."""
        result = []
        to_remove = []
        for sid, session in _terminal_sessions.items():
            if not _owns_terminal(session, request):
                continue
            if not _session_is_alive(session):
                to_remove.append(sid)
                continue
            result.append({
                "id": sid,
                "created_at": session["created_at"],
                "pid": session["pid"],
            })
        for sid in to_remove:
            await _cleanup_session(sid)
        return result


    @app.get("/api/terminals/{session_id}", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def get_terminal(session_id: str, request: Request):
        """Get info about a terminal session."""
        session = _terminal_sessions.get(session_id)
        if session is None or not _owns_terminal(session, request):
            return JSONResponse({"error": "Session not found"}, status_code=404)
        if not _session_is_alive(session):
            await _cleanup_session(session_id)
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return {
            "id": session_id,
            "created_at": session["created_at"],
            "pid": session["pid"],
        }


    @app.delete("/api/terminals/{session_id}", dependencies=[Depends(verify_api_key)], include_in_schema=False)
    async def delete_terminal(session_id: str, request: Request):
        """Kill and remove a terminal session."""
        if session_id not in _terminal_sessions or not _owns_terminal(_terminal_sessions[session_id], request):
            return JSONResponse({"error": "Session not found"}, status_code=404)
        await _cleanup_session(session_id)
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
            await _cleanup_session(session_id)
            await ws.close(code=4004, reason="Session has ended")
            return

        await ws.accept()

        # First-message authentication
        try:
            msg = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
            payload = json.loads(msg)
            if not isinstance(payload, dict) or payload.get("type") != "auth" or (API_KEY and not matches_api_key(payload.get("token"))):
                await ws.close(code=4001, reason="Invalid API key")
                return
        except (asyncio.TimeoutError, ValueError, AttributeError, KeyError, WebSocketDisconnect):
            await ws.close(code=4001, reason="Auth timeout or invalid payload")
            return
        if (
            session["user_id"] != ws.headers.get("x-user-id", "")
            or session["chat_id"] != ws.headers.get("x-session-id", payload.get("chat_id", ""))
        ):
            await ws.close(code=4004, reason="Session not found")
            return

        backend = session["backend"]
        if session["connected"]:
            await ws.close(code=4009, reason="Session already connected")
            return
        session["connected"] = True
        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        # --- Platform-specific read/write/resize helpers ---

        if backend == "pty":
            master_fd = session["master_fd"]
            process = session["process"]

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

            def _do_resize(rows: int, cols: int):
                pty_proc.setwinsize(rows, cols)

        # --- Reader / writer tasks ---

        async def _pty_reader():
            """Forward PTY output -> WebSocket."""
            while not stop_event.is_set():
                data = await read_pty(master_fd) if backend == "pty" else await loop.run_in_executor(None, _blocking_read)
                if not data:
                    break
                session["output"].extend(data)
                del session["output"][:-65536]
                await ws.send_bytes(data)

        async def _ws_reader():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                elif "bytes" in msg and msg["bytes"]:
                    await _write_terminal(session, msg["bytes"])
                elif "text" in msg and msg["text"]:
                    try:
                        payload = json.loads(msg["text"])
                        if payload.get("type") == "resize":
                            cols = payload.get("cols", 80)
                            rows = payload.get("rows", 24)
                            _do_resize(rows, cols)
                    except (json.JSONDecodeError, KeyError):
                        pass
        tasks = (asyncio.create_task(_pty_reader()), asyncio.create_task(_ws_reader()))
        session["io_tasks"] = tasks
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_event.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await _cleanup_session(session_id)
            try:
                await ws.close()
            except (WebSocketDisconnect, RuntimeError):
                pass


# ---------------------------------------------------------------------------
# Notebook execution (optional)
# ---------------------------------------------------------------------------

if ENABLE_NOTEBOOKS:
    from open_terminal.utils import notebooks

    app.include_router(notebooks.router)
