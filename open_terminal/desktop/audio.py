"""Desktop sound, streamed as WebM/Opus to the browser."""

import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import sys

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
        capture = None
        tasks = []
        try:
            source, capture = await self.source()
            process = await desktop.spawn(
                "ffmpeg", "-nostdin", "-loglevel", "error",
                *source,
                "-ar", "48000", "-ac", "2",
                "-c:a", "libopus", "-b:a", "64k", "-vbr", "off",
                "-application", "lowdelay", "-frame_duration", "20",
                "-f", "webm", "-live", "1", "-cluster_time_limit", "100",
                "-flush_packets", "1", "pipe:1",
                stdin=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )

            async def outgoing():
                while data := await process.stdout.read(4096):
                    await asyncio.wait_for(ws.send_bytes(data), 2)

            async def incoming():
                while (await ws.receive())["type"] != "websocket.disconnect":
                    pass

            tasks = [asyncio.create_task(outgoing()), asyncio.create_task(incoming())]
            if hasattr(desktop, "closed"):
                async def sharing_ended():
                    while not desktop.closed:
                        await asyncio.sleep(.1)
                tasks.append(asyncio.create_task(sharing_ended()))
            if capture:
                async def feed():
                    while data := await capture.stdout.read(8192):
                        process.stdin.write(data)
                        await asyncio.wait_for(process.stdin.drain(), 2)
                tasks.append(asyncio.create_task(feed()))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for completed in done:
                completed.result()
        finally:
            for child in tasks:
                child.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            async def stop(child):
                # A full stdout pipe can keep asyncio's process.wait() pending.
                async def drain():
                    while await child.stdout.read(65536):
                        pass
                draining = asyncio.create_task(drain())
                try:
                    await desktop.stop_process(child)
                    await draining
                finally:
                    draining.cancel()
                    await asyncio.gather(draining, return_exceptions=True)
            await asyncio.gather(*(stop(child) for child in (process, capture) if child))
            desktop.peers.discard(ws)
            self.streams.discard(task)

    async def source(self):
        return ["-f", "pulse", "-sample_rate", "48000", "-channels", "2",
                "-fragment_size", "3840", "-i", "desktop.monitor"], None

    async def close(self):
        for task in list(self.streams):
            task.cancel()
        await asyncio.gather(*self.streams, return_exceptions=True)
        if self.process:
            await self.desktop.stop_process(self.process)
            self.process = None


class HostAudio(DesktopAudio):
    def __init__(self, desktop):
        super().__init__(desktop)
        self.enabled = False

    @property
    def available(self):
        return self.enabled and not getattr(self.desktop, "closed", False)

    async def monitor(self):
        # Resolve an output's monitor explicitly; the default source may be a mic.
        name = (await self.command("pactl", "get-default-sink")).decode().strip()
        sinks = json.loads(await self.command("pactl", "--format=json", "list", "sinks"))
        for sink in sinks:
            if sink["name"] == name and sink.get("monitor_source"):
                return sink["monitor_source"]
        raise RuntimeError("No monitor for the desktop's default audio output")

    async def start(self):
        if not shutil.which("ffmpeg"):
            return
        try:
            if sys.platform == "linux":
                if not shutil.which("pactl"):
                    return
                # SSH often has a different runtime directory from the desktop.
                socket = Path(f"/run/user/{os.getuid()}/pulse/native")
                if socket.is_socket() and socket.stat().st_uid == os.getuid():
                    self.desktop.env["PULSE_SERVER"] = f"unix:{socket}"
                await self.monitor()
            elif sys.platform == "darwin":
                if int(platform.mac_ver()[0].split(".")[0]) < 13:
                    return
                if importlib.util.find_spec("ScreenCaptureKit") is None:
                    return
            elif sys.platform == "win32":
                if importlib.util.find_spec("soundcard") is None:
                    return
            else:
                return
            self.enabled = True
        except (OSError, RuntimeError, TimeoutError, ValueError, KeyError):
            log.warning("Host audio unavailable; continuing without sound", exc_info=True)

    async def source(self):
        if sys.platform == "linux":
            # Re-select on unmute, including after a speaker/headphone change.
            return ["-f", "pulse", "-sample_rate", "48000", "-channels", "2",
                    "-fragment_size", "3840", "-i", await self.monitor()], None
        capture = await self.desktop.spawn(
            sys.executable, "-u", "-m", "open_terminal.desktop.audio",
            stdout=asyncio.subprocess.PIPE,
        )
        return ["-f", "f32le", "-ar", "48000", "-ac", "2", "-i", "pipe:0"], capture

    async def close(self):
        self.enabled = False
        await super().close()


def windows_capture():
    import soundcard

    speaker = soundcard.default_speaker()
    source = soundcard.get_microphone(id=speaker.id, include_loopback=True)
    if not source.isloopback or source.id != speaker.id:
        raise RuntimeError("No WASAPI loopback device for the default output")
    # WASAPI's shared mixer converts the output to stereo, including surround.
    with source.recorder(samplerate=48000, channels=2, blocksize=960) as recorder:
        while True:
            samples = recorder.record(numframes=960)
            sys.stdout.buffer.write(samples.astype("<f4").tobytes())


def macos_capture():
    from array import array
    import queue
    import time

    import CoreMedia as CM
    import ScreenCaptureKit as SC
    from Foundation import NSDate, NSObject, NSRunLoop
    import objc

    packets = queue.Queue(maxsize=50)
    errors = []

    class AudioOutput(NSObject, protocols=[objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")]):
        def stream_didOutputSampleBuffer_ofType_(self, stream, sample, kind):
            if kind != SC.SCStreamOutputTypeAudio or not CM.CMSampleBufferDataIsReady(sample):
                return
            try:
                rate, format_id, flags, _, _, _, channels, bits, _ = CM.CMAudioFormatDescriptionGetStreamBasicDescription(CM.CMSampleBufferGetFormatDescription(sample))
                if (rate != 48000 or channels != 2 or bits != 32
                        or format_id != int.from_bytes(b"lpcm", "big") or not flags & 1 or flags & 2):
                    raise RuntimeError("ScreenCaptureKit returned an unsupported audio format")
                block = CM.CMSampleBufferGetDataBuffer(sample)
                size = CM.CMBlockBufferGetDataLength(block)
                status, data = CM.CMBlockBufferCopyDataBytes(block, 0, size, None)
                frames = CM.CMSampleBufferGetNumSamples(sample)
                if status or size != frames * 8:
                    raise RuntimeError("ScreenCaptureKit returned an invalid audio buffer")
                if flags & 32:  # Non-interleaved float planes.
                    planar = array("f", data)
                    stereo = array("f", [0]) * (frames * 2)
                    stereo[0::2], stereo[1::2] = planar[:frames], planar[frames:]
                    data = stereo.tobytes()
                if packets.full():
                    packets.get_nowait()
                packets.put_nowait(data)
            except Exception as exc:
                errors.append(str(exc))

        def stream_didStopWithError_(self, stream, error):
            errors.append(str(error))

    def wait_result(invoke):
        results = []
        invoke(lambda *args: results.append(args))
        deadline = time.monotonic() + 15
        while not results:
            if time.monotonic() > deadline:
                raise RuntimeError("ScreenCaptureKit timed out; check screen/audio recording permission")
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.05))
        if results[0][-1] is not None:
            raise RuntimeError(str(results[0][-1]))
        return results[0][0]

    content = wait_result(SC.SCShareableContent.getShareableContentWithCompletionHandler_)
    if not content.displays():
        raise RuntimeError("No logged-in macOS display available for audio capture")
    filter = SC.SCContentFilter.alloc().initWithDisplay_excludingWindows_(content.displays()[0], [])
    config = SC.SCStreamConfiguration.alloc().init()
    config.setCapturesAudio_(True)
    config.setSampleRate_(48000)
    config.setChannelCount_(2)
    config.setExcludesCurrentProcessAudio_(True)
    config.setWidth_(2)
    config.setHeight_(2)
    config.setMinimumFrameInterval_(CM.CMTimeMake(1, 1))
    output = AudioOutput.alloc().init()
    stream = SC.SCStream.alloc().initWithFilter_configuration_delegate_(filter, config, output)
    ok, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(output, SC.SCStreamOutputTypeAudio, None, None)
    if not ok:
        raise RuntimeError(str(error))
    wait_result(stream.startCaptureWithCompletionHandler_)
    try:
        while not errors:
            with objc.autorelease_pool():
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.02))
                while not packets.empty():
                    sys.stdout.buffer.write(packets.get_nowait())
        raise RuntimeError(errors[0])
    finally:
        stream.stopCaptureWithCompletionHandler_(None)


if __name__ == "__main__":
    try:
        if sys.platform == "darwin":
            macos_capture()
        elif sys.platform == "win32":
            windows_capture()
        else:
            raise RuntimeError("Native host audio helper requires macOS or Windows")
    except BrokenPipeError:
        pass
    except Exception as exc:
        print(f"Host audio capture failed: {exc}", file=sys.stderr)
        sys.exit(1)
