"""Bounded binary image updates shared by native desktops and Chrome."""

import asyncio
from collections import deque
import io
import struct
import time

from PIL import Image, ImageChops


class FrameWindow:
    """Start with one frame until a viewer opts into ordered pipelining."""

    def __init__(self):
        self.limit = 1
        self.pending = 0
        self.packets = 1
        self.times = deque()
        self.delay = 0.0
        self.ready = asyncio.Event()
        self.ready.set()

    async def wait(self):
        await self.ready.wait()

    def sent(self, packets=1):
        # A frame may be several packets, each acknowledged on its own.
        self.packets = packets
        self.pending += packets
        self.times.extend([time.monotonic()] * packets)
        if self.pending >= self.limit * packets:
            self.ready.clear()

    def ack(self, window=1):
        if type(window) is not int or not 1 <= window <= 3:
            raise ValueError("Invalid frame window")
        self.limit = window
        self.pending = max(0, self.pending - 1)
        if self.times:
            self.delay += (time.monotonic() - self.times.popleft() - self.delay) / 8
        if self.pending < self.limit * self.packets:
            self.ready.set()
        else:
            self.ready.clear()


class Frames:
    TILE = 64
    MAX_REGIONS = 8
    SETTLE = 0.3

    def __init__(self):
        self.previous = None
        self.source = None
        self.soft = None
        self.changed = 0.0

    def packets(self, image, quality=75):
        """Encode each changed area separately, then sharpen them once still."""
        boxes = []
        if image is not self.source:
            self.source = image
            boxes = self.regions(image.convert("RGB"))
        if boxes:
            self.changed = time.monotonic()
            for box in boxes:
                self.soft = box if self.soft is None else (
                    min(self.soft[0], box[0]), min(self.soft[1], box[1]),
                    max(self.soft[2], box[2]), max(self.soft[3], box[3]),
                )
            return [self.packet(box, quality=quality) for box in boxes]
        if self.soft is not None and time.monotonic() - self.changed >= self.SETTLE:
            box, self.soft = self.soft, None
            return [self.packet(box, quality=max(90, quality), subsampling=0)]
        return []

    def regions(self, image):
        previous, self.previous = self.previous, image
        if previous is None or previous.size != image.size:
            self.soft = None
            return [(0, 0, *image.size)]
        difference = ImageChops.difference(previous, image)
        bounds = difference.getbbox()
        if bounds is None:
            return []
        left, top, right, bottom = bounds
        regions, above = [], {}
        for y in range(top, bottom, self.TILE):
            end = min(y + self.TILE, bottom)
            spans = []
            for x in range(left, right, self.TILE):
                edge = min(x + self.TILE, right)
                if difference.crop((x, y, edge, end)).getbbox() is None:
                    continue
                if spans and spans[-1][1] == x:
                    spans[-1] = (spans[-1][0], edge)
                else:
                    spans.append((x, edge))
            # Extend a region downwards while its horizontal span stays the same.
            above = {span: (above[span][0] if span in above else y, end) for span in spans}
            regions = [r for r in regions if not (r[3] == y and (r[0], r[2]) in above)]
            regions += [(x, start, edge, end) for (x, edge), (start, end) in above.items()]
        return regions if len(regions) <= self.MAX_REGIONS else [bounds]

    def packet(self, box, **options):
        width, height = self.previous.size
        left, top, right, bottom = box
        output = io.BytesIO()
        self.previous.crop(box).save(output, "JPEG", **options)
        return struct.pack("!6H", width, height, left, top, right - left, bottom - top) + output.getvalue()

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
