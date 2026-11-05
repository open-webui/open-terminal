"""Bounded binary image updates shared by native desktops and Chrome."""

import asyncio
import io
import struct

from PIL import Image, ImageChops


class FrameWindow:
    """Start with one frame until a viewer opts into ordered pipelining."""

    def __init__(self):
        self.limit = 1
        self.pending = 0
        self.ready = asyncio.Event()
        self.ready.set()

    async def wait(self):
        await self.ready.wait()

    def sent(self):
        self.pending += 1
        if self.pending >= self.limit:
            self.ready.clear()

    def ack(self, window=1):
        if type(window) is not int or not 1 <= window <= 3:
            raise ValueError("Invalid frame window")
        self.limit = window
        self.pending = max(0, self.pending - 1)
        if self.pending < self.limit:
            self.ready.set()
        else:
            self.ready.clear()


class Frames:
    def __init__(self):
        self.previous = None

    def encode(self, image, quality=75, original=None):
        image = image.convert("RGB")
        width, height = image.size
        box = (0, 0, width, height)
        if self.previous is not None and self.previous.size == image.size:
            box = ImageChops.difference(self.previous, image).getbbox()
            if box is None:
                return None
        self.previous = image
        left, top, right, bottom = box
        if original and (right - left) * (bottom - top) > width * height // 2:
            # Reuse Chrome's JPEG for large updates instead of compressing it twice.
            return struct.pack("!6H", width, height, 0, 0, width, height) + original
        output = io.BytesIO()
        image.crop(box).save(output, "JPEG", quality=quality)
        return (
            struct.pack("!6H", width, height, left, top, right - left, bottom - top)
            + output.getvalue()
        )

    def jpeg(self, data, quality=75):
        with Image.open(io.BytesIO(data)) as image:
            return self.encode(image, quality, original=data)
