"""Owned browser sessions and capability-scoped iframe/stream endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import secrets
import posixpath
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import websockets
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response, StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from open_terminal.dependencies import get_filesystem, verify_api_key
from open_terminal.utils.fs import UserFS
from .rewrite import (
    proxy_path,
    rewrite_css,
    rewrite_html,
    rewrite_javascript,
    target_url,
)

router = APIRouter(prefix="/browser", include_in_schema=False)


@dataclass
class Session:
    id: str
    token: str
    owner: str
    chat: str
    fs: UserFS
    url: str = ""
    title: str = ""
    mode: str = "proxy"
    chrome: object | None = None
    touched: float = field(default_factory=time.monotonic)
    peers: set = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserCORSMiddleware(CORSMiddleware):
    async def __call__(self, scope, receive, send):
        if scope.get("path", "").startswith("/browser/view/"):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)


sessions: dict[str, Session] = {}
reaper: asyncio.Task | None = None
IDLE_TIMEOUT = 24 * 60 * 60


async def remove(session):
    sessions.pop(session.id, None)
    for peer in list(session.peers):
        with contextlib.suppress(Exception):
            await peer.close(code=4004)
    async with session.lock:
        if session.chrome:
            await session.chrome.close()


async def reap_idle():
    while True:
        await asyncio.sleep(60)
        for session in list(sessions.values()):
            if time.monotonic() - session.touched > IDLE_TIMEOUT:
                await remove(session)


profiles: dict[str, httpx.AsyncClient] = {}
HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
SKIP_RESPONSE = HOP | {
    "content-encoding",
    "content-length",
    "content-security-policy",
    "content-security-policy-report-only",
    "x-frame-options",
    "set-cookie",
    "access-control-allow-origin",
    "access-control-allow-credentials",
}


def owner(request):
    return request.headers.get("x-user-id") or "single"


def owned(request, sid):
    session = sessions.get(sid)
    if (
        not session
        or session.owner != owner(request)
        or session.chat != request.headers.get("x-session-id", "")
    ):
        raise HTTPException(404, "Browser session not found")
    session.touched = time.monotonic()
    return session


def capability(sid, token):
    session = sessions.get(sid)
    if not session or not hmac.compare_digest(session.token.encode(), token.encode()):
        raise HTTPException(404, "Browser session expired")
    session.touched = time.monotonic()
    return session


def payload(session):
    from .chrome import find_browser

    return {
        "id": session.id,
        "token": session.token,
        "url": session.url,
        "title": session.title,
        "mode": session.mode,
        "chrome_available": find_browser() is not None,
    }


class CreateSession(BaseModel):
    url: str = Field(default="", max_length=16384)


class UpdateSession(BaseModel):
    url: str | None = Field(default=None, max_length=16384)
    title: str | None = Field(default=None, max_length=512)
    mode: str | None = None


def valid_url(url):
    try:
        return target_url(url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sessions", dependencies=[Depends(verify_api_key)])
async def create(
    request: Request, body: CreateSession, fs: UserFS = Depends(get_filesystem)
):
    global reaper
    if reaper is None or reaper.done():
        reaper = asyncio.create_task(reap_idle())
    if sum(s.owner == owner(request) for s in sessions.values()) >= 16:
        raise HTTPException(409, "Close a browser tab before opening another")
    if body.url:
        valid_url(body.url)
    session = Session(
        secrets.token_hex(16),
        secrets.token_urlsafe(32),
        owner(request),
        request.headers.get("x-session-id", ""),
        fs,
        url=body.url,
    )
    sessions[session.id] = session
    if session.owner not in profiles:
        profiles[session.owner] = httpx.AsyncClient(
            follow_redirects=False, timeout=httpx.Timeout(60, connect=10)
        )
    return payload(session)


@router.get("/sessions/{sid}", dependencies=[Depends(verify_api_key)])
async def get_session(request: Request, sid: str):
    return payload(owned(request, sid))


@router.patch("/sessions/{sid}", dependencies=[Depends(verify_api_key)])
async def update(request: Request, sid: str, body: UpdateSession):
    session = owned(request, sid)
    if body.mode not in (None, "proxy", "chrome"):
        raise HTTPException(400, "Invalid browser mode")
    if body.url:
        valid_url(body.url)
    async with session.lock:
        if body.url is not None:
            session.url = body.url
        if body.title is not None:
            session.title = body.title
        if body.mode == "chrome" and session.chrome is None:
            from .chrome import Chrome

            try:
                session.chrome = await Chrome.create(session)
            except Exception as exc:
                raise HTTPException(409, str(exc)) from exc
        elif body.mode == "proxy" and session.chrome is not None:
            await session.chrome.close()
            session.chrome = None
        if body.mode:
            session.mode = body.mode
    return payload(session)


@router.delete("/sessions/{sid}", dependencies=[Depends(verify_api_key)])
async def close(request: Request, sid: str):
    session = owned(request, sid)
    await remove(session)
    return {"status": "closed"}


async def shutdown():
    global reaper
    if reaper:
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)
        reaper = None
    await asyncio.gather(
        *(s.chrome.close() for s in sessions.values() if s.chrome),
        return_exceptions=True,
    )
    sessions.clear()
    await asyncio.gather(
        *(p.aclose() for p in profiles.values()), return_exceptions=True
    )
    profiles.clear()
    from .chrome import shutdown_hosts

    await shutdown_hosts()


def view_prefix(request, sid, token):
    # Relative references survive both direct access and the WebUI relay unchanged.
    return posixpath.relpath(
        f"/browser/view/{sid}/{token}", posixpath.dirname(request.url.path)
    )


def response_headers(upstream):
    return {
        **{k: v for k, v in upstream.headers.items() if k.lower() not in SKIP_RESPONSE},
        "access-control-allow-origin": "*",
        "referrer-policy": "no-referrer",
        "cache-control": "no-store",
        # Keep remote pages isolated even when their URL is opened outside the iframe.
        "content-security-policy": "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads",
    }


@router.get("/view/{sid}/{token}/runtime.js")
async def runtime(sid: str, token: str):
    capability(sid, token)
    return Response(
        Path(__file__).with_name("runtime.js").read_text(),
        media_type="application/javascript",
        headers={"access-control-allow-origin": "*", "cache-control": "no-store"},
    )


@router.api_route(
    "/view/{sid}/{token}/frame/{scheme}/{host}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def frame(
    request: Request, sid: str, token: str, scheme: str, host: str, path: str
):
    session = capability(sid, token)
    url = valid_url(
        urlunsplit((scheme, host, "/" + path, str(request.query_params), ""))
    )
    prefix = view_prefix(request, sid, token)
    if request.method == "OPTIONS" and request.headers.get(
        "access-control-request-method"
    ):
        return Response(
            headers={
                "access-control-allow-origin": "*",
                "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,HEAD,OPTIONS",
                "access-control-allow-headers": request.headers.get(
                    "access-control-request-headers", ""
                ),
            }
        )
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower()
        not in HOP
        | {
            "host",
            "cookie",
            "authorization",
            "accept-encoding",
            "content-length",
            "origin",
            "referer",
        }
        and not k.lower().startswith("x-")
    }
    headers["accept-encoding"] = "gzip, deflate"
    if request.headers.get("origin"):
        headers["origin"] = f"{scheme}://{host}"
    client = profiles[session.owner]
    body = request.stream() if request.headers.get("content-length") or request.headers.get("transfer-encoding") else None
    try:
        upstream = await client.send(
            client.build_request(
                request.method, url, headers=headers, content=body
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Cannot connect to this website") from exc
    output_headers = response_headers(upstream)
    if location := upstream.headers.get("location"):
        output_headers["location"] = proxy_path(prefix, urljoin(url, location))
    content_type = upstream.headers.get("content-type", "")
    rewrite = any(
        t in content_type
        for t in ("text/html", "application/xhtml", "text/css", "javascript")
    )
    if rewrite:
        try:
            data = bytearray()
            async for chunk in upstream.aiter_bytes():
                data.extend(chunk)
                if len(data) > 16 * 1024 * 1024:
                    raise HTTPException(413, "Browser resource exceeds 16 MiB")
            text = data.decode(upstream.encoding or "utf-8", errors="replace")
            if "html" in content_type:
                text = rewrite_html(text, url, prefix, url)
            elif "css" in content_type:
                text = rewrite_css(text, url, prefix)
            else:
                text = rewrite_javascript(text, url, prefix)
            output_headers["content-type"] = (
                content_type.split(";")[0] + "; charset=utf-8"
            )
            return Response(
                text, status_code=upstream.status_code, headers=output_headers
            )
        finally:
            await upstream.aclose()

    async def stream():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        stream(), status_code=upstream.status_code, headers=output_headers
    )


@router.websocket("/view/{sid}/{token}/ws")
async def proxy_socket(ws: WebSocket, sid: str, token: str):
    try:
        session = capability(sid, token)
    except HTTPException:
        await ws.close(code=4004)
        return
    await ws.accept()
    session.peers.add(ws)
    tasks = []
    try:
        message = await asyncio.wait_for(ws.receive_json(), 10)
        url = str(message.get("url", ""))
        parsed = urlsplit(url)
        if parsed.scheme not in ("ws", "wss"):
            raise ValueError("Invalid WebSocket URL")
        http_url = valid_url(
            urlunsplit(
                (
                    "https" if parsed.scheme == "wss" else "http",
                    parsed.netloc,
                    parsed.path,
                    parsed.query,
                    "",
                )
            )
        )
        protocols = message.get("protocols") or []
        if isinstance(protocols, str):
            protocols = [protocols]
        prepared = profiles[session.owner].build_request("GET", http_url)
        headers = (
            {"cookie": prepared.headers["cookie"]}
            if "cookie" in prepared.headers
            else None
        )
        async with websockets.connect(
            url,
            subprotocols=protocols,
            additional_headers=headers,
            max_size=16 * 1024 * 1024,
        ) as upstream:
            await ws.send_json({"type": "open", "protocol": upstream.subprotocol or ""})

            async def send():
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    await upstream.send(
                        msg.get("bytes")
                        if msg.get("bytes") is not None
                        else msg["text"]
                    )

            async def receive():
                async for msg in upstream:
                    await (
                        ws.send_bytes(msg)
                        if isinstance(msg, bytes)
                        else ws.send_text(msg)
                    )

            tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (
        WebSocketDisconnect,
        ValueError,
        TimeoutError,
        httpx.HTTPError,
        websockets.WebSocketException,
    ):
        with contextlib.suppress(Exception):
            await ws.close(code=1011)
    finally:
        session.peers.discard(ws)
        with contextlib.suppress(Exception):
            await ws.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/view/{sid}/{token}/stream")
@router.websocket("/view/{sid}/{token}/vnc")
async def chrome_socket(ws: WebSocket, sid: str, token: str):
    try:
        session = capability(sid, token)
    except HTTPException:
        await ws.close(code=4004)
        return
    if not session.chrome:
        await ws.close(code=4004)
        return
    vnc = ws.url.path.endswith("/vnc")
    if vnc and not session.chrome.host.display:
        await ws.close(code=4004)
        return
    session.peers.add(ws)
    try:
        await (session.chrome.stream_vnc(ws) if vnc else session.chrome.stream(ws))
    finally:
        session.peers.discard(ws)
