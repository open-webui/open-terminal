import asyncio
import json
import os
import platform
import socket
import sys
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from open_terminal.env import API_KEY


def get_system_info() -> str:
    """Gather runtime system metadata for the OpenAPI description."""
    shell = os.environ.get("SHELL", "/bin/sh")
    lines = [
        f"- **OS:** {platform.system()} {platform.release()} ({platform.machine()})",
        f"- **Hostname:** {socket.gethostname()}",
        f"- **Shell:** {shell}",
        f"- **Python:** {sys.version.split()[0]}",
        f"- **Working Directory:** {os.getcwd()}",
    ]
    return "\n".join(lines)


_EXECUTE_DESCRIPTION = (
    "Run a shell command and return the result.\n\n"
    "**Environment:**\n"
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
    description="Shell command execution API with synchronous and streaming support.",
    version="0.1.4",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExecRequest(BaseModel):
    command: str = Field(
        ...,
        description="Shell command to execute. Supports chaining (&&, ||, ;), pipes (|), and redirections.",
        json_schema_extra={"examples": ["echo hello", "ls -la && whoami"]},
    )
    timeout: Optional[float] = Field(
        30.0,
        description="Max execution time in seconds. Process is killed if exceeded (exit_code: -1). Null to disable.",
        ge=0,
    )


class ExecResponse(BaseModel):
    exit_code: int = Field(
        ...,
        description="Process exit code. 0 = success, non-zero = error, -1 = timeout.",
    )
    stdout: str = Field(..., description="Captured standard output.")
    stderr: str = Field(..., description="Captured standard error.")


@app.get(
    "/health",
    summary="Health check",
    description="Returns service status. No authentication required.",
)
async def health():
    return {"status": "ok"}



# Temporary links: {token: (path, expiry_timestamp)}
_download_links: dict[str, tuple[str, float]] = {}
_upload_links: dict[str, tuple[str, float]] = {}


@app.get(
    "/files/download/link",
    summary="Get a file download link",
    description="Returns a temporary download URL for a file. Link expires after 5 minutes and requires no authentication to use.",
    dependencies=[Depends(verify_api_key)],
    responses={
        404: {"description": "File not found."},
        401: {"description": "Invalid or missing API key."},
    },
)
async def get_file_link(
    path: str = Query(..., description="Absolute path to the file."),
    request: Request = None,
):
    import time
    import uuid

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    token = uuid.uuid4().hex
    _download_links[token] = (path, time.time() + 300)

    base_url = str(request.base_url).rstrip("/")
    return {"url": f"{base_url}/files/download/{token}"}


@app.get(
    "/files/download/{token}",
    include_in_schema=False,
)
async def download_file(token: str):
    import time

    entry = _download_links.pop(token, None)
    if not entry:
        raise HTTPException(status_code=404, detail="Invalid or expired download link")

    path, expiry = entry
    if time.time() > expiry:
        raise HTTPException(status_code=404, detail="Download link expired")

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(path)


@app.post(
    "/files/upload",
    summary="Upload a file",
    description="Save a file to the specified path. Provide a `url` to fetch remotely, or send the file directly via multipart form data.",
    dependencies=[Depends(verify_api_key)],
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def upload_file(
    dir: str = Query(..., description="Destination directory for the file."),
    url: Optional[str] = Query(None, description="URL to download the file from. If omitted, expects a multipart file upload."),
    file: Optional[UploadFile] = File(None, description="The file to upload (if no URL provided)."),
):
    if url:
        import httpx
        from urllib.parse import urlparse

        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        content = resp.content
        filename = os.path.basename(urlparse(url).path) or "download"
    elif file:
        content = await file.read()
        filename = file.filename or "upload"
    else:
        raise HTTPException(status_code=400, detail="Provide either 'url' or a file upload.")

    os.makedirs(dir, exist_ok=True)
    path = os.path.join(dir, filename)
    with open(path, "wb") as f:
        f.write(content)
    return {"path": path, "size": len(content)}


@app.post(
    "/files/upload/link",
    summary="Create an upload link",
    description="Generate a temporary, unauthenticated upload URL. Link expires after 5 minutes.",
    dependencies=[Depends(verify_api_key)],
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def create_upload_link(
    dir: str = Query(..., description="Destination directory for the uploaded file."),
    request: Request = None,
):
    import time
    import uuid

    token = uuid.uuid4().hex
    _upload_links[token] = (dir, time.time() + 300)

    base_url = str(request.base_url).rstrip("/")
    return {"url": f"{base_url}/files/upload/{token}"}


@app.get(
    "/files/upload/{token}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def upload_page(token: str):
    import time

    entry = _upload_links.get(token)
    if not entry or time.time() > entry[1]:
        return HTMLResponse("Link expired.", status_code=404)

    return HTMLResponse(
        '<form method="post" enctype="multipart/form-data">'
        '<input type="file" name="file" required> '
        '<button type="submit">Upload</button>'
        '</form>'
    )


@app.post(
    "/files/upload/{token}",
    include_in_schema=False,
)
async def upload_file_via_link(
    token: str,
    file: UploadFile = File(..., description="The file to upload."),
):
    import time

    entry = _upload_links.pop(token, None)
    if not entry:
        raise HTTPException(status_code=404, detail="Invalid or expired upload link")

    dir, expiry = entry
    if time.time() > expiry:
        raise HTTPException(status_code=404, detail="Upload link expired")

    filename = file.filename or "upload"
    os.makedirs(dir, exist_ok=True)
    path = os.path.join(dir, filename)
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)
    return {"path": path, "size": len(content)}


@app.post(
    "/execute",
    summary="Execute a command",
    description=_EXECUTE_DESCRIPTION,
    dependencies=[Depends(verify_api_key)],
    response_model=ExecResponse,
    responses={
        401: {"description": "Invalid or missing API key."},
    },
)
async def execute(
    req: ExecRequest,
    stream: bool = Query(
        False,
        description="Stream output as JSONL (application/x-ndjson) instead of waiting for completion.",
    ),
):
    if stream:
        return _stream_response(req)

    try:
        proc = await asyncio.create_subprocess_shell(
            req.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=req.timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return ExecResponse(
            exit_code=-1,
            stdout="",
            stderr=f"Command timed out after {req.timeout}s",
        )

    return ExecResponse(
        exit_code=proc.returncode or 0,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )


def _stream_response(req: ExecRequest):
    async def generate():
        proc = await asyncio.create_subprocess_shell(
            req.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def read_stream(s, label):
            async for line in s:
                yield json.dumps(
                    {"type": label, "data": line.decode(errors="replace")}
                ) + "\n"

        async for chunk in read_stream(proc.stdout, "stdout"):
            yield chunk
        async for chunk in read_stream(proc.stderr, "stderr"):
            yield chunk

        await proc.wait()
        yield json.dumps({"type": "exit", "data": proc.returncode}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")
