"""Owned native agent processes, output replay and lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException, WebSocket
from pydantic import BaseModel, Field

from open_terminal.env import sanitized_environ
from . import codex, claude_code, cursor, grok, opencode, cline, gemini, pi

LIMIT = 16 * 1024 * 1024
PROVIDERS = {
    "codex": codex,
    "claude-code": claude_code,
    "cursor": cursor,
    "grok": grok,
    "opencode": opencode,
    "cline": cline,
    "gemini": gemini,
    "pi": pi,
}
sessions: dict[str, Session] = {}
creation_lock = asyncio.Lock()


class Start(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    cwd: str | None = None
    model: str | None = Field(default=None, max_length=256)
    resume: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    instructions: str = Field(default="", max_length=65536)


def identity(fs):
    env = sanitized_environ()
    env.pop("CLAUDECODE", None)
    kwargs = {}
    prefix = []
    home = str(Path.home())
    if fs.username:
        import pwd

        account = pwd.getpwnam(fs.username)
        home = account.pw_dir
        # Never pass the server account's provider credentials to another OS user.
        for key in list(env):
            if key.startswith(
                (
                    "OPENAI_",
                    "ANTHROPIC_",
                    "CLAUDE_",
                    "CODEX_",
                    "GOOGLE_",
                    "GEMINI_",
                    "CURSOR_",
                    "XAI_",
                    "GROK_",
                    "OPENCODE_",
                    "CLINE_",
                    "PI_",
                )
            ):
                env.pop(key, None)
        env.update(HOME=home, USER=fs.username, LOGNAME=fs.username)
        if os.getuid() == 0:
            kwargs.update(user=account.pw_uid, group=account.pw_gid, extra_groups=[])
        else:
            prefix = ["sudo", "-H", "-n", "-u", fs.username, "--"]
    env["PATH"] = os.pathsep.join(
        [
            *(
                str(Path(home) / p)
                for p in (".local/bin", ".cursor/bin", ".opencode/bin")
            ),
            env.get("PATH", ""),
        ]
    )
    return env, kwargs, prefix


def command(provider, fs, options):
    env, kwargs, prefix = identity(fs)
    adapter = PROVIDERS[provider]
    configured = os.environ.get(
        getattr(
            adapter, "COMMAND_ENV", f"OPEN_TERMINAL_{adapter.BINARY.upper()}_COMMAND"
        )
    )
    executable = configured or shutil.which(adapter.BINARY, path=env["PATH"])
    if not executable:
        raise HTTPException(409, f"{adapter.NAME} is not installed in this environment")
    return [*prefix, executable, *adapter.arguments(options)], env, kwargs


async def spawn(provider, fs, options):
    cwd = fs.resolve_path(options.cwd or fs.home)
    fs._check_path(cwd)
    if not await fs.isdir(cwd):
        raise HTTPException(400, "Agent working directory does not exist")
    args, env, kwargs = command(provider, fs, options)
    return await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        **kwargs,
        start_new_session=os.name != "nt",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=LIMIT,
    ), cwd


async def stop_process(proc):
    if proc.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(proc.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


@dataclass
class Session:
    id: str
    owner: str
    chat: str
    provider: str
    options: Start
    process: asyncio.subprocess.Process
    cwd: str
    output: deque = field(default_factory=deque)
    size: int = 0
    sequence: int = 0
    acknowledged: int = 0
    input_sequence: int = 0
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    peer: WebSocket | None = None
    tasks: list = field(default_factory=list)
    stderr: deque = field(default_factory=lambda: deque(maxlen=20))
    touched: float = field(default_factory=time.monotonic)
    error: str | None = None
    ended: bool = False

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                data = line.decode("utf-8")
                # The relay limit includes the JSON carrier's escaping and framing.
                if (
                    len(
                        json.dumps(
                            {
                                "type": "output",
                                "sequence": self.sequence + 1,
                                "data": data,
                            }
                        ).encode()
                    )
                    > LIMIT
                ):
                    raise ValueError("Agent output frame is too large")
                # JSONL is the native framing for both providers.
                json.loads(data)
                async with self.condition:
                    await self.condition.wait_for(
                        lambda: self.size + len(line) <= LIMIT
                    )
                    self.sequence += 1
                    self.output.append((self.sequence, data, len(line)))
                    self.size += len(line)
                    self.condition.notify_all()
            await self.process.wait()
        except Exception as exc:
            self.error = f"Agent protocol failed: {type(exc).__name__}"
        finally:
            async with self.condition:
                self.ended = True
                self.condition.notify_all()

    async def drain_stderr(self):
        while data := await self.process.stderr.read(4096):
            self.stderr.append(data.decode(errors="replace"))

    async def acknowledge(self, cursor):
        if type(cursor) is not int or not self.acknowledged <= cursor <= self.sequence:
            raise ValueError("Invalid agent output cursor")
        async with self.condition:
            self.acknowledged = cursor
            while self.output and self.output[0][0] <= cursor:
                self.size -= self.output.popleft()[2]
            self.condition.notify_all()


async def remove(session):
    sessions.pop(session.id, None)
    if session.peer:
        with contextlib.suppress(Exception):
            await session.peer.close(code=4004)
    await stop_process(session.process)
    for task in session.tasks:
        task.cancel()
    await asyncio.gather(*session.tasks, return_exceptions=True)


async def shutdown():
    await asyncio.gather(*(remove(s) for s in list(sessions.values())))


async def maintain():
    # Detached processes may be busy. Allow reconnect; never apply the PTY's short timeout.
    for session in list(sessions.values()):
        if not session.peer and time.monotonic() - session.touched > 3600:
            await remove(session)
