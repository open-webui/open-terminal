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
        # The lowest delay in the current and previous few seconds.
        self.lows = [float("inf"), float("inf")]
        self.rotated = float("-inf")
        self.level = 0  # 0 is full quality, 2 the lightest.
        self.hold = 5.0
        self.changed = self.calm = self.raised = float("-inf")
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
            now = time.monotonic()
            sample = now - self.times.popleft()
            self.delay += (sample - self.delay) / 8
            if now - self.rotated > 10:
                self.lows, self.rotated = [sample, self.lows[0]], now
            self.lows[0] = min(self.lows[0], sample)
        if self.pending < self.limit * self.packets:
            self.ready.set()
        else:
            self.ready.clear()


    @property
    def queued(self):
        """Delay beyond the path's own, so a distant link is not a slow one."""
        return max(0.0, self.delay - min(self.lows))

    def adapt(self):
        """Step quality down when frames queue, and back up once they stop."""
        now = time.monotonic()
        if self.queued > 0.1 and self.level < 2 and now - self.changed > 3:
            # A step up that did not last makes the next attempt wait longer.
            self.hold = min(60.0, self.hold * 2) if now - self.raised < 10 else 5.0
            self.level += 1
            self.changed = now
        if self.queued > 0.04:
            self.calm = now
        elif self.level and now - max(self.calm, self.changed) > self.hold:
            self.level -= 1
            self.changed = self.raised = now
        return self.level


class Frames:
    TILE = 64
    MAX_REGIONS = 8
    VIDEO_SHARE = 20  # One part in this many must change to use video.
    SETTLE = 0.3
    QUALITY = (75, 60, 45)
    STEADY = 8

    def __init__(self):
        self.previous = None
        self.source = None
        self.soft = None
        self.changed = 0.0
        self.motion = 0  # Video frames in a row.

    def packets(self, image, level=0, video=None):
        """Encode each changed area separately, then sharpen them once still.

        Large changes use `video` when given; it returns None to fall back to JPEG.
        """
        boxes = []
        if image is not self.source:
            self.source = image
            rgb = image.convert("RGB")
            # Comparing frames costs as much as encoding, so in steady motion do it
            # only every few frames, which also notices when the motion stops.
            steady = self.motion >= self.STEADY and self.motion % self.STEADY
            if steady and video is not None and rgb.size == self.previous.size:
                self.previous, boxes = rgb, [(0, 0, *rgb.size)]
            else:
                boxes = self.regions(rgb)
        if boxes:
            self.changed = time.monotonic()
            width, height = self.previous.size
            area = sum((right - left) * (bottom - top) for left, top, right, bottom in boxes)
            if video is not None and area * self.VIDEO_SHARE >= width * height:
                packet = video.encode(self.previous, level)
                if packet is not None:
                    self.motion += 1
                    self.soft = (0, 0, width, height)
                    return [packet]
            self.motion = 0
            for box in boxes:
                self.soft = box if self.soft is None else (
                    min(self.soft[0], box[0]), min(self.soft[1], box[1]),
                    max(self.soft[2], box[2]), max(self.soft[3], box[3]),
                )
            return [self.packet(box, quality=self.QUALITY[level]) for box in boxes]
        if self.soft is not None and time.monotonic() - self.changed >= self.SETTLE:
            box, self.soft = self.soft, None
            self.motion = 0
            return [self.packet(box, quality=75 if level else 90, subsampling=0)]
        return []

    def reset(self):
        """Send the whole screen again next time."""
        self.previous = self.source = self.soft = None
        self.motion = 0

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
