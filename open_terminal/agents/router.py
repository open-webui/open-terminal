"""Authenticated agent session HTTP and native JSONL WebSocket endpoints."""

import asyncio
import contextlib
import json
import time
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket

from open_terminal.dependencies import get_filesystem, matches_api_key, verify_api_key
from open_terminal.env import API_KEY
from . import discovery, sessions as runtime

router = APIRouter(prefix="/agents", tags=["agents"])
router.add_api_route(
    "",
    discovery.discover,
    methods=["GET"],
    dependencies=[Depends(verify_api_key)],
    include_in_schema=False,
)


def owned(request, sid):
    session = runtime.sessions.get(sid)
    if (
        not session
        or session.owner != (request.headers.get("x-user-id") or "single")
        or session.chat != request.headers.get("x-session-id", "")
    ):
        raise HTTPException(404, "Agent session not found")
    session.touched = time.monotonic()
    return session


def payload(session):
    return {
        "id": session.id,
        "agent": session.provider,
        "cwd": session.cwd,
        "running": session.process.returncode is None,
        "protocol_version": 1,
    }


@router.post(
    "/{provider}/sessions",
    dependencies=[Depends(verify_api_key)],
    include_in_schema=False,
)
async def create(
    provider: Literal[
        "codex", "claude-code", "cursor", "grok", "opencode", "cline", "gemini", "pi"
    ],
    body: runtime.Start,
    request: Request,
):
    fs = get_filesystem(request)
    owner = request.headers.get("x-user-id") or "single"
    chat = request.headers.get("x-session-id", "")
    if not chat:
        raise HTTPException(400, "X-Session-Id is required")
    async with runtime.creation_lock:
        for session in runtime.sessions.values():
            if (session.owner, session.chat, session.options.request_id) == (
                owner,
                chat,
                body.request_id,
            ):
                if session.provider != provider or session.options != body:
                    raise HTTPException(
                        409, "Agent request already exists with different options"
                    )
                return payload(session)
            if (session.owner, session.chat) == (owner, chat):
                raise HTTPException(
                    409, "Another agent turn is running in this conversation"
                )
        if sum(s.owner == owner for s in runtime.sessions.values()) >= 16:
            raise HTTPException(429, "Too many agent sessions")
        process, cwd = await runtime.spawn(provider, fs, body)
        session = runtime.Session(
            uuid4().hex, owner, chat, provider, body, process, cwd
        )
        runtime.sessions[session.id] = session
        session.tasks = [
            asyncio.create_task(session.read()),
            asyncio.create_task(session.drain_stderr()),
        ]
        return payload(session)


@router.get(
    "/sessions/{sid}", dependencies=[Depends(verify_api_key)], include_in_schema=False
)
async def status(sid: str, request: Request):
    return payload(owned(request, sid))


@router.delete(
    "/sessions/{sid}", dependencies=[Depends(verify_api_key)], include_in_schema=False
)
async def delete(sid: str, request: Request):
    await runtime.remove(owned(request, sid))
    return {"status": True}


@router.websocket("/sessions/{sid}/connect")
async def connect(ws: WebSocket, sid: str):
    await ws.accept()
    session = None
    tasks = []
    try:
        auth = await asyncio.wait_for(ws.receive_json(), 10)
        if auth.get("type") != "auth" or (
            API_KEY and not matches_api_key(auth.get("token"))
        ):
            await ws.close(code=4401)
            return
        session = owned(ws, sid)
        if session.peer:
            await ws.close(code=4409)
            return
        cursor = auth.get("cursor", 0)
        await session.acknowledge(cursor)
        session.peer = ws

        async def receive():
            while True:
                frame = await ws.receive_json()
                session.touched = time.monotonic()
                if frame.get("type") == "ack":
                    await session.acknowledge(frame.get("sequence"))
                elif frame.get("type") == "input":
                    sequence, data = frame.get("sequence"), frame.get("data")
                    if (
                        type(sequence) is not int
                        or sequence < 1
                        or sequence > session.input_sequence + 1
                    ):
                        raise ValueError("Invalid agent input sequence")
                    if not isinstance(data, str) or len(data.encode()) > runtime.LIMIT:
                        raise ValueError("Invalid agent input")
                    if sequence == session.input_sequence + 1:
                        for line in data.split("\n"):
                            if line.strip():
                                json.loads(line)
                        session.process.stdin.write((data.rstrip("\n") + "\n").encode())
                        # Record before yielding: reconnect cannot repeat a written command.
                        session.input_sequence = sequence
                        await session.process.stdin.drain()
                    await ws.send_json({"type": "ack", "sequence": sequence})
                elif frame.get("type") == "eof":
                    session.process.stdin.close()
                else:
                    raise ValueError("Unknown agent transport frame")

        async def send():
            sent = cursor
            while True:
                async with session.condition:
                    await session.condition.wait_for(
                        lambda: session.sequence > sent or session.ended
                    )
                    frames = [frame for frame in session.output if frame[0] > sent]
                for seq, data, _ in frames:
                    await ws.send_json(
                        {"type": "output", "sequence": seq, "data": data}
                    )
                    sent = seq
                if session.ended:
                    await ws.send_json(
                        {
                            "type": "exit",
                            "code": session.process.returncode,
                            "error": session.error,
                        }
                    )
                    return

        tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except Exception:
        with contextlib.suppress(Exception):
            await ws.close(code=4400)
    finally:
        if session and session.peer is ws:
            session.peer = None
            session.touched = time.monotonic()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
