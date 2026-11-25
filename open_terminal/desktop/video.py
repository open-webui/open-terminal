"""Optional low-delay H.264 for desktop motion, using the FFmpeg on PATH."""

import logging
import queue
import struct
import subprocess
import sys
import threading

from open_terminal.env import sanitized_environ

log = logging.getLogger(__name__)


class VideoEncoder:
    """One encoder per viewer. Each frame goes in and its packet comes straight out."""

    RATES = (("23", "12M", "1200k"), ("27", "4M", "400k"), ("31", "1500k", "300k"))

    def __init__(self):
        self.process = None
        self.packets = None
        self.size = None
        self.level = 0
        self.failed = False

    def encode(self, image, level=0):
        """Return a viewer packet, or None when the caller should send JPEG."""
        if self.failed:
            return None
        try:
            # Changing the bitrate restarts FFmpeg, which costs one full frame.
            if self.process is None or (image.size, level) != (self.size, self.level):
                self.start(image.size, level)
            process, packets = self.process, self.packets
            if process is None:
                raise OSError("The viewer disconnected")
            data = memoryview(image.tobytes())
            while data:
                data = data[process.stdin.write(data):]
            packet = packets.get(timeout=3)
            if packet is None:
                raise OSError("FFmpeg stopped; H.264 needs FFmpeg with libx264")
        except (OSError, ValueError, queue.Empty) as exc:
            if not self.failed:
                log.info("Desktop H.264 unavailable, using JPEG: %s", str(exc) or "FFmpeg did not respond")
            self.close()
            return None
        config, frame = packet
        # A zero-sized region marks video; a decoder config marks a key frame.
        return struct.pack("!7H", *image.size, 0, 0, 0, 0, len(config)) + config + frame

    def start(self, size, level):
        self.stop()
        self.size, self.level = size, level
        rate = self.RATES[level]
        self.process = subprocess.Popen(
            [
                "ffmpeg", "-loglevel", "quiet", "-probesize", "32", "-analyzeduration", "0",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size", f"{size[0]}x{size[1]}",
                "-framerate", "60", "-i", "pipe:0", "-an",
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-threads", "4",
                "-crf", rate[0], "-maxrate", rate[1], "-bufsize", rate[2],
                "-g", "100000", "-sc_threshold", "0",
                # FLV gives exact packet sizes and the AVC config that WebCodecs expects.
                "-f", "flv", "-flush_packets", "1", "pipe:1",
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            env=sanitized_environ(),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if self.failed:  # Closed from another thread while starting.
            self.stop()
            return
        self.packets = queue.Queue()
        threading.Thread(
            target=self.read, args=(self.process.stdout, self.packets), daemon=True, name="desktop-video",
        ).start()

    @staticmethod
    def read(stream, packets):
        def exactly(size):
            data = bytearray()
            while len(data) < size:
                chunk = stream.read(size - len(data))
                if not chunk:
                    raise EOFError
                data += chunk
            return bytes(data)

        config = b""
        try:
            exactly(13)  # FLV header
            while True:
                header = exactly(11)
                body = exactly(int.from_bytes(header[1:4], "big"))
                exactly(4)  # Previous tag size
                if header[0] == 9 and body[1] == 0:
                    config = body[5:]
                elif header[0] == 9 and body[1] == 1:
                    packets.put((config, body[5:]))
                    config = b""
        except (EOFError, OSError, ValueError, IndexError):
            packets.put(None)

    def stop(self):
        process, self.process = self.process, None
        if process is not None:
            process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()

    def close(self):
        self.failed = True
        self.stop()
