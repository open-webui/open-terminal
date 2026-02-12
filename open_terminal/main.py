import asyncio
import json
import os
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from open_terminal.env import API_KEY

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
    version="0.1.3",
    dependencies=[Depends(verify_api_key)],
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



# Temporary download links: {token: (path, expiry_timestamp)}
_download_links: dict[str, tuple[str, float]] = {}


@app.get(
    "/files",
    summary="Get a file download link",
    description="Returns a temporary download URL for a file. Link expires after 5 minutes and requires no authentication to use.",
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
    summary="Download via link",
    description="Download a file using a temporary token. No authentication required.",
    responses={
        404: {"description": "Invalid or expired download link."},
    },
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
    "/execute",
    summary="Execute a command",
    description="Run a shell command and return the result.",
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
