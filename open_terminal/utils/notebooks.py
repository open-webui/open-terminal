"""Jupyter notebook execution endpoints.

Provides per-cell execution with multi-session support. Each session gets its
own Jupyter kernel via nbclient. Requires the ``notebooks`` optional extra::

    pip install open-terminal[notebooks]
"""

import asyncio
import json
import os
import subprocess
import time
import uuid
from typing import Optional

import aiofiles
import aiofiles.os
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

import nbformat
from nbclient import NotebookClient
from jupyter_client.manager import AsyncKernelManager


class _UserKernelManager(AsyncKernelManager):
    """Launch the notebook kernel as a specific OS user (multi-user isolation).

    In multi-user mode the kernel must run as the requesting user, not as the
    privileged server process, so cell code is confined to that user's own
    files by the OS (mirroring how commands run via ``sudo -u``). The Jupyter
    connection file is made group-readable to that user so the kernel can read
    it, while the server (its owner) can still reconcile connection info.
    """

    run_as_user = None
    run_as_home = None

    async def _async_launch_kernel(self, kernel_cmd, **kw):
        if self.run_as_user:
            cf = self.connection_file
            subprocess.run(["sudo", "chgrp", self.run_as_user, cf], check=True)
            subprocess.run(["sudo", "chmod", "640", cf], check=True)
            kernel_cmd = [
                "sudo", "-n", "-u", self.run_as_user,
                "env", f"HOME={self.run_as_home}", f"PATH={os.environ.get('PATH', '')}",
                *kernel_cmd,
            ]
        return await super()._async_launch_kernel(kernel_cmd, **kw)


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------

_IDLE_TIMEOUT = 30 * 60  # 30 minutes


class _Session:
    """Wraps a NotebookClient for a specific notebook."""

    __slots__ = ("id", "path", "nb", "client", "busy", "created_at", "last_used", "owner")

    def __init__(self, session_id: str, path: str, nb, client, owner=None):
        self.id = session_id
        self.path = path
        self.nb = nb
        self.client = client
        self.owner = owner
        self.busy = False
        self.created_at = time.time()
        self.last_used = time.time()


_sessions: dict[str, _Session] = {}
_cleanup_task: Optional[asyncio.Task] = None


async def _idle_cleanup_loop():
    """Periodically remove sessions idle for more than _IDLE_TIMEOUT."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        stale = [
            sid
            for sid, s in _sessions.items()
            if now - s.last_used > _IDLE_TIMEOUT and not s.busy
        ]
        for sid in stale:
            await _destroy_session(sid)


async def _destroy_session(session_id: str):
    session = _sessions.pop(session_id, None)
    if session and session.client:
        try:
            await session.client._async_cleanup_kernel()
        except Exception:
            pass


def _ensure_cleanup_task():
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_idle_cleanup_loop())


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    path: str = Field(..., description="Absolute path to the .ipynb file.")


class CreateSessionResponse(BaseModel):
    id: str
    kernel: str
    status: str


class ExecuteCellRequest(BaseModel):
    cell_index: int = Field(..., description="Zero-based cell index to execute.")
    source: Optional[str] = Field(
        None, description="Override cell source. If omitted, uses the source already in the notebook."
    )


class ExecuteCellResponse(BaseModel):
    status: str
    execution_count: Optional[int] = None
    outputs: list = Field(default_factory=list)


class SessionStatusResponse(BaseModel):
    id: str
    path: str
    kernel: str
    status: str


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def create_notebooks_router(verify_api_key, get_filesystem) -> APIRouter:
    """Create the notebooks router with the given auth dependency."""

    router = APIRouter(
        prefix="/notebooks",
        tags=["notebooks"],
        dependencies=[Depends(verify_api_key)],
    )



    @router.post(
        "",
        response_model=CreateSessionResponse,
        operation_id="create_notebook_session",
        summary="Create a notebook session",
        description="Start a Jupyter kernel for the given notebook. Returns a session ID for subsequent execute calls.",
        include_in_schema=False,
    )
    async def create_session(req: CreateSessionRequest, http_request: Request):

        _ensure_cleanup_task()

        # Enforce the same per-user boundary as the file APIs: reject a notebook
        # path that points into another user's home.
        fs = get_filesystem(http_request)
        owner = http_request.headers.get("x-user-id")
        path = os.path.abspath(req.path)
        if not fs.is_path_allowed(path):
            raise HTTPException(
                status_code=403,
                detail=f"Access denied: {path} belongs to another user",
            )
        if not await aiofiles.os.path.isfile(path):
            raise HTTPException(status_code=404, detail=f"Notebook not found: {path}")

        # Read and parse the notebook
        async with aiofiles.open(path, encoding="utf-8") as f:
            content = await f.read()

        try:
            nb = nbformat.reads(content, as_version=4)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid notebook: {e}")

        kernel_name = nb.metadata.get("kernelspec", {}).get("name", "python3")

        # Start kernel. In multi-user mode run the kernel as the requesting OS
        # user so cell code is confined to that user's files.
        client = NotebookClient(nb, kernel_name=kernel_name, timeout=120)
        try:
            if fs.username:
                km = _UserKernelManager(kernel_name=kernel_name)
                km.run_as_user = fs.username
                km.run_as_home = fs.home
                km.connection_file = f"/tmp/otnb-{uuid.uuid4().hex}.json"
                client.km = km
            else:
                client.create_kernel_manager()
            start_kwargs = {"cwd": fs.home} if fs.username else {}
            await client.async_start_new_kernel(**start_kwargs)
            await client.async_start_new_kernel_client()
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to start kernel '{kernel_name}': {e}",
            )

        session_id = uuid.uuid4().hex[:12]
        _sessions[session_id] = _Session(session_id, path, nb, client, owner=owner)

        return CreateSessionResponse(
            id=session_id, kernel=kernel_name, status="ready"
        )

    @router.post(
        "/{session_id}/execute",
        response_model=ExecuteCellResponse,
        operation_id="execute_notebook_cell",
        summary="Execute a notebook cell",
        description="Execute a single cell in the given session. Optionally override the cell source. "
        "Updates the .ipynb file in place after execution.",
        include_in_schema=False,
    )
    async def execute_cell(session_id: str, req: ExecuteCellRequest, http_request: Request):


        session = _sessions.get(session_id)
        if not session or session.owner != http_request.headers.get("x-user-id"):
            raise HTTPException(status_code=404, detail="Session not found")
        if session.busy:
            raise HTTPException(status_code=409, detail="Cell already executing")

        nb = session.nb
        if req.cell_index < 0 or req.cell_index >= len(nb.cells):
            raise HTTPException(
                status_code=400,
                detail=f"cell_index {req.cell_index} out of range (0..{len(nb.cells) - 1})",
            )

        cell = nb.cells[req.cell_index]
        if req.source is not None:
            cell.source = req.source

        session.busy = True
        session.last_used = time.time()

        try:
            await session.client.async_execute_cell(cell, req.cell_index)
        except Exception as e:
            session.busy = False
            # Return the error as a cell output rather than HTTP error
            return ExecuteCellResponse(
                status="error",
                outputs=[
                    {
                        "output_type": "error",
                        "ename": type(e).__name__,
                        "evalue": str(e),
                        "traceback": [str(e)],
                    }
                ],
            )

        session.busy = False
        session.last_used = time.time()

        # Serialize outputs
        outputs = []
        for o in cell.outputs:
            od = dict(o)
            if "data" in od:
                od["data"] = dict(od["data"])
            outputs.append(od)

        ec = cell.get("execution_count")

        # Save notebook to disk
        try:
            nb_json = nbformat.writes(nb)
            async with aiofiles.open(session.path, "w", encoding="utf-8") as f:
                await f.write(nb_json)
        except Exception:
            pass  # non-fatal

        return ExecuteCellResponse(
            status="ok", execution_count=ec, outputs=outputs
        )

    @router.get(
        "/{session_id}",
        response_model=SessionStatusResponse,
        operation_id="get_notebook_session",
        summary="Get notebook session status",
        include_in_schema=False,
    )
    async def get_session(session_id: str, http_request: Request):
        session = _sessions.get(session_id)
        if not session or session.owner != http_request.headers.get("x-user-id"):
            raise HTTPException(status_code=404, detail="Session not found")

        kernel_name = session.nb.metadata.get("kernelspec", {}).get(
            "name", "python3"
        )
        status = "busy" if session.busy else "ready"
        return SessionStatusResponse(
            id=session.id, path=session.path, kernel=kernel_name, status=status
        )

    @router.delete(
        "/{session_id}",
        operation_id="delete_notebook_session",
        summary="Stop a notebook session",
        include_in_schema=False,
    )
    async def delete_session(session_id: str, http_request: Request):
        session = _sessions.get(session_id)
        if not session or session.owner != http_request.headers.get("x-user-id"):
            raise HTTPException(status_code=404, detail="Session not found")
        await _destroy_session(session_id)
        return {"status": "stopped"}

    return router
