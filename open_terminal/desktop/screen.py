"""Mac screen capture helper: writes raw BGRX frames to stdout as the screen changes."""

import os
import sys
import threading
import time


def pixels(sample):
    """The frame's pixels without row padding, or None when nothing new was drawn."""
    import CoreMedia as CM
    import Quartz
    import ScreenCaptureKit as SC

    if not CM.CMSampleBufferDataIsReady(sample):
        return None
    info = CM.CMSampleBufferGetSampleAttachmentsArray(sample, False)
    if not info or info[0].get(SC.SCStreamFrameInfoStatus) != SC.SCFrameStatusComplete:
        return None
    buffer = CM.CMSampleBufferGetImageBuffer(sample)
    if buffer is None or Quartz.CVPixelBufferLockBaseAddress(buffer, Quartz.kCVPixelBufferLock_ReadOnly):
        return None
    try:
        row, height = Quartz.CVPixelBufferGetBytesPerRow(buffer), Quartz.CVPixelBufferGetHeight(buffer)
        used = Quartz.CVPixelBufferGetWidth(buffer) * 4
        data = bytes(Quartz.CVPixelBufferGetBaseAddress(buffer).as_buffer(row * height))
    finally:
        Quartz.CVPixelBufferUnlockBaseAddress(buffer, Quartz.kCVPixelBufferLock_ReadOnly)
    return data if row == used else b"".join(data[i:i + used] for i in range(0, row * height, row))


def capture(width, height, fps, output):
    import CoreMedia as CM
    import Quartz
    import ScreenCaptureKit as SC
    from Foundation import NSDate, NSObject, NSRunLoop
    import objc

    # Also connects to the window server, which ScreenCaptureKit needs.
    display_id = Quartz.CGMainDisplayID()
    bounds = Quartz.CGDisplayBounds(display_id)
    parent = os.getppid()
    ready = threading.Event()
    latest, errors = [None], []

    class ScreenOutput(NSObject, protocols=[objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")]):
        def stream_didOutputSampleBuffer_ofType_(self, stream, sample, kind):
            try:
                if kind == SC.SCStreamOutputTypeScreen and (data := pixels(sample)) is not None:
                    latest[0] = data
                    ready.set()
            except Exception as exc:
                errors.append(str(exc))
                ready.set()

        def stream_didStopWithError_(self, stream, error):
            errors.append(str(error))
            ready.set()

    def wait_result(invoke):
        results = []
        invoke(lambda *args: results.append(args))
        deadline = time.monotonic() + 10
        while not results:
            if time.monotonic() > deadline:
                raise RuntimeError("ScreenCaptureKit timed out")
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.02))
        if results[0][-1] is not None:
            raise RuntimeError(str(results[0][-1]))
        return results[0][0]

    content = wait_result(SC.SCShareableContent.getShareableContentWithCompletionHandler_)
    display = next((d for d in content.displays() if d.displayID() == display_id), None)
    if display is None:
        raise RuntimeError("The main display cannot be captured")
    config = SC.SCStreamConfiguration.alloc().init()
    config.setWidth_(width)
    config.setHeight_(height)
    config.setPixelFormat_(Quartz.kCVPixelFormatType_32BGRA)
    config.setColorSpaceName_(Quartz.kCGColorSpaceSRGB)
    config.setMinimumFrameInterval_(CM.CMTimeMake(1, fps))
    config.setQueueDepth_(3)
    config.setShowsCursor_(True)
    target = ScreenOutput.alloc().init()
    stream = SC.SCStream.alloc().initWithFilter_configuration_delegate_(
        SC.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, []), config, target,
    )
    ok, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(target, SC.SCStreamOutputTypeScreen, None, None)
    if not ok:
        raise RuntimeError(str(error))
    wait_result(stream.startCaptureWithCompletionHandler_)
    while os.getppid() == parent:
        if ready.wait(1):
            ready.clear()
            if errors:
                raise RuntimeError(errors[0])
            data, latest[0] = latest[0], None
            if data is not None:
                output.write(data)
                output.flush()
        if Quartz.CGMainDisplayID() != display_id or Quartz.CGDisplayBounds(display_id) != bounds:
            return  # The parent starts again at the new size.


if __name__ == "__main__":
    try:
        capture(*map(int, sys.argv[1:4]), sys.stdout.buffer)
    except BrokenPipeError:
        pass
    except Exception as exc:
        print(f"Screen capture failed: {exc}", file=sys.stderr)
        sys.exit(1)
