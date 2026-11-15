"""Private virtual-desktop sound, streamed as WebM/Opus to the browser."""

import asyncio
import json
import logging
import shutil

log = logging.getLogger(__name__)


class DesktopAudio:
    def __init__(self, desktop):
        self.desktop = desktop
        self.process = None
        self.streams = set()

    @property
    def available(self):
        return self.process is not None and self.process.returncode is None

    async def command(self, *args):
        process = await self.desktop.spawn(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, error = await asyncio.wait_for(process.communicate(), 5)
            if process.returncode:
                raise RuntimeError(error.decode(errors="replace")[-1000:])
            return output
        finally:
            await self.desktop.stop_process(process)

    async def start(self, *, pipewire=False):
        desktop = self.desktop
        socket = desktop.runtime / "audio.sock"
        # Keep apps off the host audio server even if optional audio setup fails.
        desktop.env.update(PULSE_SERVER=f"unix:{socket}", PULSE_SINK="desktop", PULSE_SOURCE="desktop.monitor")
        server = "pipewire-pulse" if pipewire else "pulseaudio"
        if not all(shutil.which(name) for name in (server, "ffmpeg", "pactl")):
            return
        try:
            if pipewire:
                config = desktop.runtime / "audio.conf"
                config.write_text(json.dumps({
                    "context.spa-libs": {"audio.convert.*": "audioconvert/libspa-audioconvert", "support.*": "support/libspa-support"},
                    "context.modules": [{"name": "libpipewire-module-" + name} for name in (
                        "protocol-native", "client-node", "adapter", "metadata", "protocol-pulse",
                    )],
                    "pulse.properties": {"server.address": [f"unix:{socket}"]},
                    "pulse.cmd": [{"cmd": "load-module", "args": "module-null-sink sink_name=desktop rate=48000 channels=2"}],
                }))
                # Use only this generated config, without host/user drop-ins.
                command = ("env", f"PIPEWIRE_CONFIG_DIR={desktop.runtime}", "PIPEWIRE_CONFIG_PREFIX=",
                           "pipewire-pulse", "--config", config.name)
            else:
                command = (
                    "pulseaudio", "--daemonize=no", "--use-pid-file=no", "--exit-idle-time=-1",
                    "--disable-shm=yes", "--log-target=stderr", "-n",
                    "--load=module-null-sink sink_name=desktop rate=48000 channels=2",
                    f"--load=module-native-protocol-unix socket={socket} auth-cookie-enabled=0",
                )
            with (desktop.runtime / "audio.log").open("wb") as output:
                self.process = await desktop.spawn(*command, stdout=output, stderr=output)
            for _ in range(50):
                if self.process.returncode is not None:
                    raise RuntimeError("Desktop audio server exited")
                if socket.is_socket():
                    await self.command("pactl", "info")
                    await self.command("pactl", "set-default-sink", "desktop")
                    await self.command("pactl", "set-default-source", "desktop.monitor")
                    return
                await asyncio.sleep(0.1)
            raise RuntimeError("Desktop audio server did not start")
        except (OSError, RuntimeError, TimeoutError):
            log.warning("Desktop audio unavailable; continuing without sound", exc_info=True)
            await self.close()

    async def stream(self, ws):
        desktop = self.desktop
        task = asyncio.current_task()
        self.streams.add(task)
        desktop.peers.add(ws)
        process = None
        tasks = []
        try:
            process = await desktop.spawn(
                "ffmpeg", "-nostdin", "-loglevel", "error",
                "-f", "pulse", "-sample_rate", "48000", "-channels", "2",
                "-fragment_size", "3840", "-i", "desktop.monitor",
                "-c:a", "libopus", "-b:a", "64k", "-vbr", "off",
                "-application", "lowdelay", "-frame_duration", "20",
                "-f", "webm", "-live", "1", "-cluster_time_limit", "100",
                "-flush_packets", "1", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )

            async def outgoing():
                while data := await process.stdout.read(4096):
                    await asyncio.wait_for(ws.send_bytes(data), 2)

            async def incoming():
                while (await ws.receive())["type"] != "websocket.disconnect":
                    pass

            tasks = [asyncio.create_task(outgoing()), asyncio.create_task(incoming())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for completed in done:
                completed.result()
        finally:
            for child in tasks:
                child.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process:
                await desktop.stop_process(process)
            desktop.peers.discard(ws)
            self.streams.discard(task)

    async def close(self):
        for task in list(self.streams):
            task.cancel()
        await asyncio.gather(*self.streams, return_exceptions=True)
        if self.process:
            await self.desktop.stop_process(self.process)
            self.process = None
