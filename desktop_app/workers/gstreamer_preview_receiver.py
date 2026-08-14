import time
from collections import deque
from threading import Lock

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage


def inspect_host_gstreamer():
    """Return capabilities without making GStreamer a mandatory dependency."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
    except Exception as exc:
        return {"available": False, "error": f"Python GStreamer bindings unavailable: {exc}"}
    required = ("udpsrc", "rtph264depay", "h264parse", "videoconvert", "appsink")
    missing = [name for name in required if Gst.ElementFactory.find(name) is None]
    decoder = _find_h264_decoder(Gst)
    if missing or decoder is None:
        details = ", ".join(missing) or "H.264 decoder"
        return {"available": False, "version": Gst.version_string(), "error": f"Missing Host GStreamer component: {details}"}
    return {"available": True, "version": Gst.version_string(), "decoder": decoder}


def _find_h264_decoder(Gst):
    factories = Gst.ElementFactory.list_get_elements(Gst.ELEMENT_FACTORY_TYPE_DECODER, Gst.Rank.NONE)
    matches = []
    for factory in factories:
        if "Video" not in factory.get_klass():
            continue
        for template in factory.get_static_pad_templates():
            if template.direction == Gst.PadDirection.SINK and "video/x-h264" in template.get_caps().to_string():
                matches.append(factory)
                break
    matches.sort(key=lambda item: item.get_rank(), reverse=True)
    return matches[0].get_name() if matches else None


class GStreamerPreviewReceiver(QThread):
    connected = Signal()
    frame_received = Signal()
    failed = Signal(str)

    def __init__(self, port, decoder, parent=None):
        super().__init__(parent)
        self.port = port
        self.decoder = decoder
        self._stopping = False
        self._pipeline = None
        self._frame_lock = Lock()
        self._latest_frame = None
        self._notification_pending = False
        self._last_frame_time = None

    def stop(self):
        self._stopping = True

    def take_latest_frame(self):
        with self._frame_lock:
            result = self._latest_frame
            self._latest_frame = None
            self._notification_pending = False
        return result

    def run(self):
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            pipeline_text = (
                f'udpsrc port={self.port} caps="application/x-rtp,media=video,encoding-name=H264,payload=96" '
                f'! rtpjitterbuffer latency=50 drop-on-latency=true ! rtph264depay ! h264parse '
                f'! {self.decoder} ! videoconvert ! video/x-raw,format=RGB '
                '! appsink name=preview_sink emit-signals=true sync=false max-buffers=1 drop=true'
            )
            pipeline = Gst.parse_launch(pipeline_text)
            self._pipeline = pipeline
            sink = pipeline.get_by_name("preview_sink")
            sink.connect("new-sample", self._on_sample, Gst)
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Host GStreamer pipeline failed to enter PLAYING")
            self.connected.emit()
            bus = pipeline.get_bus()
            started = time.monotonic()
            while not self._stopping:
                message = bus.timed_pop_filtered(
                    100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS
                )
                if message is not None:
                    if message.type == Gst.MessageType.ERROR:
                        error, debug = message.parse_error()
                        raise RuntimeError(f"Host GStreamer error: {error}; {debug or ''}")
                    raise RuntimeError("Host GStreamer pipeline reached EOS")
                with self._frame_lock:
                    last_frame = self._last_frame_time
                if time.monotonic() - (last_frame or started) > 4.0:
                    raise TimeoutError("No decoded H.264 preview frame for 4 seconds")
        except Exception as exc:
            if not self._stopping:
                self.failed.emit(str(exc))
        finally:
            pipeline = self._pipeline
            self._pipeline = None
            if pipeline is not None:
                try: pipeline.set_state(Gst.State.NULL)
                except Exception: pass

    def _on_sample(self, sink, Gst):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buffer, caps = sample.get_buffer(), sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            image = QImage(bytes(mapped.data), width, height, width * 3, QImage.Format.Format_RGB888).copy()
        finally:
            buffer.unmap(mapped)
        current = time.monotonic()
        history = getattr(self, "_history", deque())
        self._history = history
        history.append(current)
        while history and current - history[0] > 1.0: history.popleft()
        fps = (len(history)-1)/(history[-1]-history[0]) if len(history) > 1 else 0.0
        received = getattr(self, "_received", 0) + 1
        self._received = received
        notify = False
        with self._frame_lock:
            self._last_frame_time = current
            self._latest_frame = (image, fps, buffer.get_size(), received)
            if not self._notification_pending:
                self._notification_pending = True; notify = True
        if notify: self.frame_received.emit()
        return Gst.FlowReturn.OK
