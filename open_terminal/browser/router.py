"""Owned browser sessions and capability-scoped iframe/stream endpoints."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import secrets
import posixpath
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import websockets
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response, StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

from open_terminal.dependencies import get_filesystem, verify_api_key
from open_terminal.env import BROWSER_MODE
from open_terminal.utils.fs import UserFS
from .rewrite import (
    proxy_path,
    rewrite_css,
    rewrite_html,
    rewrite_javascript,
    target_url,
)

router = APIRouter(prefix="/browser", tags=["browser"])


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
    active: bool = False
    last_used: float = 0
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
        "chrome_available": BROWSER_MODE == "personal" or find_browser() is not None,
    }


class CreateSession(BaseModel):
    url: str = Field(default="", max_length=16384)


class UpdateSession(BaseModel):
    url: str | None = Field(default=None, max_length=16384)
    title: str | None = Field(default=None, max_length=512)
    mode: str | None = None
    active: bool | None = None
    chat_id: str | None = Field(default=None, min_length=1, max_length=128)


def valid_url(url):
    try:
        return target_url(url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sessions", include_in_schema=False, dependencies=[Depends(verify_api_key)])
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


@router.get("/sessions/{sid}", include_in_schema=False, dependencies=[Depends(verify_api_key)])
async def get_session(request: Request, sid: str):
    return payload(owned(request, sid))


@router.patch("/sessions/{sid}", include_in_schema=False, dependencies=[Depends(verify_api_key)])
async def update(request: Request, sid: str, body: UpdateSession):
    session = owned(request, sid)
    if body.mode not in (None, "proxy", "chrome"):
        raise HTTPException(400, "Invalid browser mode")
    if body.url:
        valid_url(body.url)
    async with session.lock:
        if body.chat_id is not None:
            if session.chat and session.chat != body.chat_id:
                raise HTTPException(409, "Browser tab already belongs to a saved chat")
            session.chat = body.chat_id
        if body.active is not None:
            session.active = body.active
            if body.active:
                session.last_used = time.monotonic()
                for other in sessions.values():
                    if other is not session and (other.owner, other.chat) == (
                        session.owner,
                        session.chat,
                    ):
                        other.active = False
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


@router.delete("/sessions/{sid}", include_in_schema=False, dependencies=[Depends(verify_api_key)])
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


@router.get("/view/{sid}/{token}/runtime.js", include_in_schema=False)
async def runtime(sid: str, token: str):
    capability(sid, token)
    return Response(
        Path(__file__).with_name("runtime.js").read_text(),
        media_type="application/javascript",
        headers={"access-control-allow-origin": "*", "cache-control": "no-store"},
    )


@router.api_route(
    "/view/{sid}/{token}/frame/{scheme}/{host}/{path:path}",
    include_in_schema=False,
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
    if not session.chrome or session.chrome.detached or session.chrome.host.reader.done():
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


def visible_sessions(request):
    return [
        s
        for s in sessions.values()
        if s.owner == owner(request)
        and s.chat == request.headers.get("x-session-id", "")
    ]


def current(request, session_id=None):
    if session_id:
        return owned(request, session_id)
    candidates = visible_sessions(request)
    if not candidates:
        raise HTTPException(
            404,
            "No Browser tab is open in this chat. Open a Browser tab from the + menu.",
        )
    selected = max(candidates, key=lambda s: (s.active, s.last_used, s.touched))
    return owned(request, selected.id)


@contextlib.asynccontextmanager
async def browser_page(request, session_id=None):
    session = current(request, session_id)
    async with session.lock:
        if session.mode != "chrome" or session.chrome is None:
            raise HTTPException(
                409,
                "This Browser tab is in Proxy mode. Switch that tab to Chrome to control it.",
            )
        chrome = session.chrome
        if chrome.detached or chrome.host.reader.done():
            raise HTTPException(409, "Chrome tab disconnected. Reopen this browser tab.")
        async with chrome.host.view_lock:
            # Headed Chrome cannot reliably capture background pages.
            if chrome.host.display:
                await chrome.send("Page.bringToFront")
                await asyncio.sleep(0.1)
            try:
                yield chrome
            finally:
                viewer = chrome.host.viewer
                if chrome.host.display and viewer and viewer is not chrome:
                    with contextlib.suppress(RuntimeError):
                        await viewer.send("Page.bringToFront")


async def image(chrome):
    await chrome.state()
    result = await chrome.send(
        "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
    )
    return Response(base64.b64decode(result["data"]), media_type="image/png")


@router.get(
    "/tabs", operation_id="browser_tabs", summary="List the user's open Browser tabs", dependencies=[Depends(verify_api_key)]
)
async def tabs(request: Request):
    """List this user's open browser tabs in this chat, including their session IDs,
    URLs, titles, and modes. The selected tab is the active browser tab, or the
    last-used browser tab when hidden.
    """
    candidates = visible_sessions(request)
    for session in candidates:
        async with session.lock:
            if session.chrome:
                await session.chrome.state()
    selected = current(request).id if candidates else None
    return {
        "current_session_id": selected,
        "tabs": [
            {
                "session_id": s.id,
                "url": s.url,
                "title": s.title,
                "mode": s.mode,
                "active": s.active,
                "selected": s.id == selected,
            }
            for s in candidates
        ],
    }


@router.get(
    "/snapshot",
    operation_id="browser_snapshot",
    dependencies=[Depends(verify_api_key)],
    summary="Read the current Browser tab",
)
async def snapshot(
    request: Request, session_id: str | None = Query(default=None, max_length=128)
):
    """Return the page's accessibility tree with text, roles, values, states, and
    element refs such as @e1 for browser_action. Defaults to the active browser tab,
    or the last-used browser tab when hidden. Accepts a session_id from browser_tabs.
    """
    async with browser_page(request, session_id) as chrome:
        return await chrome.snapshot()


@router.get(
    "/screenshot",
    operation_id="browser_screenshot",
    dependencies=[Depends(verify_api_key)],
    summary="Capture a Browser tab image",
)
async def screenshot(
    request: Request, session_id: str | None = Query(default=None, max_length=128)
):
    """Return a PNG for visual inspection of layouts, canvas, and other page graphics.
    Defaults to the active
    browser tab, or the last-used browser tab when hidden. Accepts a session_id
    from browser_tabs. Action coordinates are pixels in the returned image.
    """
    async with browser_page(request, session_id) as chrome:
        return await image(chrome)


ActionType = Literal[
    "navigate",
    "back",
    "forward",
    "reload",
    "click",
    "double_click",
    "move",
    "drag",
    "scroll",
    "type",
    "key",
]


class Action(BaseModel):
    model_config = {"extra": "forbid"}

    action: ActionType
    ref: str | None = Field(
        default=None, pattern=r"^@?e[0-9]+$", max_length=64,
        description="Element ref from browser_snapshot, e.g. @e1",
    )
    x: int | None = Field(default=None, ge=0, le=16384)
    y: int | None = Field(default=None, ge=0, le=16384)
    to_x: int | None = Field(default=None, ge=0, le=16384)
    to_y: int | None = Field(default=None, ge=0, le=16384)
    button: Literal["left", "middle", "right"] = "left"
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = Field(
        default=400, ge=1, le=10000, description="Scroll distance in pixels"
    )
    text: str | None = Field(default=None, max_length=20000)
    key: str | None = Field(
        default=None,
        max_length=100,
        pattern=r"^[A-Za-z0-9_+]+$",
        description="Key or combination, e.g. Enter, Tab, ArrowDown, ctrl+a, command+a",
    )
    url: str | None = Field(default=None, max_length=16384)

    @model_validator(mode="after")
    def required_fields(self):
        if (self.x is None) != (self.y is None):
            raise ValueError("Provide both x and y")
        if self.ref and (self.x is not None or self.action in ("navigate", "back", "forward", "reload")):
            raise ValueError("Use ref for an element action, without x and y")
        if self.action in ("click", "double_click", "move", "drag") and self.x is None and not self.ref:
            raise ValueError("This action requires an element ref or x and y")
        if self.action == "drag" and (self.to_x is None or self.to_y is None):
            raise ValueError("Drag requires to_x and to_y")
        if self.action == "type" and self.text is None:
            raise ValueError("Type requires text")
        if self.action == "key" and not self.key:
            raise ValueError("Key requires a key combination")
        if self.action == "navigate" and not self.url:
            raise ValueError("Navigate requires an HTTP or HTTPS URL")
        return self


class ActionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    session_id: str | None = Field(default=None, max_length=128)
    actions: list[Action] = Field(
        min_length=1,
        max_length=50,
        description="Actions to run in order in one tab, with one final accessibility snapshot.",
    )


def key_event(value):
    parts = value.lower().split("+")
    modifiers = 0
    for part in parts[:-1]:
        flag = {
            "alt": 1,
            "ctrl": 2,
            "control": 2,
            "meta": 4,
            "cmd": 4,
            "command": 4,
            "shift": 8,
        }.get(part)
        if flag is None:
            raise HTTPException(400, "Unknown key modifier")
        modifiers |= flag
    key = parts[-1]
    special = {
        "enter": ("Enter", 13),
        "return": ("Enter", 13),
        "tab": ("Tab", 9),
        "escape": ("Escape", 27),
        "esc": ("Escape", 27),
        "backspace": ("Backspace", 8),
        "delete": ("Delete", 46),
        "space": (" ", 32),
        "arrowleft": ("ArrowLeft", 37),
        "arrowup": ("ArrowUp", 38),
        "arrowright": ("ArrowRight", 39),
        "arrowdown": ("ArrowDown", 40),
        "home": ("Home", 36),
        "end": ("End", 35),
        "pageup": ("PageUp", 33),
        "pagedown": ("PageDown", 34),
    }
    if key in special:
        name, code = special[key]
    elif len(key) == 1 and key.isascii() and key.isalnum():
        name, code = key, ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        name, code = key.upper(), 111 + int(key[1:])
    else:
        raise HTTPException(400, "Unknown key")
    event = {"key": name, "windowsVirtualKeyCode": code, "modifiers": modifiers}
    if modifiers & 6:
        command = {
            "a": "selectAll",
            "c": "copy",
            "x": "cut",
            "z": "redo" if modifiers & 8 else "undo",
        }.get(key)
        if command:
            event["commands"] = [command]
    elif name == "Enter":
        event["text"] = "\r"
    elif len(name) == 1:
        event["text"] = name.upper() if modifiers & 8 else name
    return event


async def perform_action(chrome, body):
    if body.ref:
        try:
            node = chrome.ref_node(body.ref)
            if body.action in ("type", "key"):
                await chrome.send("DOM.focus", node)
            else:
                body.x, body.y = await chrome.ref_position(node)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, f"Element ref unavailable: {exc}") from exc
    if body.action == "navigate":
        chrome.navigate(valid_url(body.url))
        await chrome.navigation
        if chrome.error:
            raise HTTPException(502, chrome.error)
    elif body.action in ("back", "forward"):
        history = await chrome.send("Page.getNavigationHistory")
        index = history["currentIndex"] + (-1 if body.action == "back" else 1)
        if 0 <= index < len(history["entries"]):
            await chrome.send(
                "Page.navigateToHistoryEntry",
                {"entryId": history["entries"][index]["id"]},
            )
    elif body.action == "reload":
        await chrome.send("Page.reload")
    elif body.action == "type":
        if body.ref:
            await chrome.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "modifiers": 2, "commands": ["selectAll"]})
            await chrome.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "modifiers": 2})
        await chrome.send("Input.insertText", {"text": body.text})
    elif body.action == "key":
        event = key_event(body.key)
        await chrome.send("Input.dispatchKeyEvent", {"type": "keyDown", **event})
        await chrome.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                **{k: v for k, v in event.items() if k not in ("text", "commands")},
            },
        )
    elif body.action == "scroll":
        delta = body.amount * (-1 if body.direction in ("up", "left") else 1)
        await chrome.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": body.x if body.x is not None else chrome.width // 2,
                "y": body.y if body.y is not None else chrome.height // 2,
                "deltaX": delta if body.direction in ("left", "right") else 0,
                "deltaY": delta if body.direction in ("up", "down") else 0,
            },
        )
    else:
        await chrome.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": body.x, "y": body.y},
        )
        if body.action != "move":
            for count in range(1, 3 if body.action == "double_click" else 2):
                try:
                    await chrome.send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mousePressed",
                            "x": body.x,
                            "y": body.y,
                            "button": body.button,
                            "clickCount": count,
                        },
                    )
                    if body.action == "drag":
                        await chrome.send(
                            "Input.dispatchMouseEvent",
                            {
                                "type": "mouseMoved",
                                "x": body.to_x,
                                "y": body.to_y,
                                "button": body.button,
                                "buttons": {"left": 1, "right": 2, "middle": 4}[
                                    body.button
                                ],
                            },
                        )
                finally:
                    await chrome.send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mouseReleased",
                            "x": body.to_x if body.action == "drag" else body.x,
                            "y": body.to_y if body.action == "drag" else body.y,
                            "button": body.button,
                            "clickCount": count,
                        },
                    )
    await asyncio.sleep(0.1)


@router.post(
    "/action",
    operation_id="browser_action",
    dependencies=[Depends(verify_api_key)],
    summary="Interact with the user's Browser tab",
)
async def action(request: Request, body: ActionRequest):
    """Batch known steps in one call; stop when you need a fresh observation.
    Executes 1–50 actions in one tab and returns one final accessibility snapshot.
    Refs may expire after page changes.
    Each item can navigate, click, drag, scroll, type, or press a key in the
    existing Chrome browser tab using refs from browser_snapshot. Typing with a ref
    replaces that input's contents; without a ref it types into the focused element.
    Pixel coordinates from browser_screenshot are also supported for visual targets.
    Defaults to the active tab, or the last-used browser tab when hidden. Set a
    session_id from browser_tabs at the top level to target one tab for the batch.
    Batches stop on failure and report completed_actions and failed_action_index
    (zero-based, null if only the final snapshot failed). Completed actions are
    not rolled back; the failed action may also have taken effect. Inspect the
    page before retrying, and do not blindly replay the batch.
    """
    actions = body.actions
    async with browser_page(request, body.session_id) as chrome:
        # Reject invalid input anywhere in the batch before acting on the page.
        for item in actions:
            for x, y in ((item.x, item.y), (item.to_x, item.to_y)):
                if x is not None and (x >= chrome.width or y is None or y >= chrome.height):
                    raise HTTPException(400, "Coordinates are outside the Browser viewport")
            if item.action == "navigate":
                valid_url(item.url)
            elif item.action == "key":
                key_event(item.key)
        completed = 0
        try:
            for item in actions:
                await perform_action(chrome, item)
                completed += 1
            return await chrome.snapshot()
        except (HTTPException, OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                exc.status_code if isinstance(exc, HTTPException) else 502,
                {
                    "completed_actions": completed,
                    "failed_action_index": completed if completed < len(actions) else None,
                    "error": exc.detail if isinstance(exc, HTTPException) else str(exc),
                },
            ) from exc
