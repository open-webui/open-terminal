"""Serve the existing ASGI API over local IPC, optionally also on TCP."""

import asyncio
import logging
import os
import sys

import uvicorn


class _PipeListener:
    # asyncio's Windows PipeServer closes synchronously and has no wait_closed.
    def __init__(self, server):
        self.server = server

    def close(self):
        self.server.close()

    async def wait_closed(self):
        pass


class IPCServer(uvicorn.Server):
    def __init__(self, config, path, http_port=None, http_host="127.0.0.1"):
        super().__init__(config)
        self.path = path
        self.http_port = http_port
        self.http_host = http_host
        self.bound_path = False

    async def startup(self, sockets=None):
        # Let Uvicorn own ASGI lifespan and shutdown, without opening a TCP socket.
        await super().startup(sockets=[])
        self.started = False
        if self.should_exit:
            return
        loop = asyncio.get_running_loop()

        def protocol():
            return self.config.http_protocol_class(
                config=self.config,
                server_state=self.server_state,
                app_state=self.lifespan.state,
            )

        try:
            if sys.platform == "win32":
                pipes = await loop.start_serving_pipe(protocol, self.path)
                self.servers.extend(_PipeListener(pipe) for pipe in pipes)
            else:
                # Never replace another process's socket (including a stale one).
                if os.path.lexists(self.path):
                    raise FileExistsError(self.path)
                server = await loop.create_unix_server(protocol, path=self.path)
                self.servers.append(server)
                self.bound_path = True
                os.chmod(self.path, 0o600)
            if self.http_port is not None:
                server = await loop.create_server(protocol, self.http_host, self.http_port)
                self.servers.append(server)
                port = server.sockets[0].getsockname()[1]
                logging.getLogger("uvicorn.error").info("HTTP: http://%s:%s", self.http_host, port)
            self.started = True
        except BaseException:
            await self.shutdown()
            raise

    async def shutdown(self, sockets=None):
        try:
            await super().shutdown(sockets=sockets)
        finally:
            if self.bound_path:
                self.bound_path = False
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass


def run_server(path, http_port=None, http_host="127.0.0.1", **options):
    # Proactor is required for named pipes and subprocesses on Windows.
    # asyncio.run uses it by default; bypass Uvicorn's optional loop selection.
    config = uvicorn.Config("open_terminal.main:app", loop="asyncio", **options)
    server = IPCServer(config, path, http_port, http_host)
    asyncio.run(server.serve())
