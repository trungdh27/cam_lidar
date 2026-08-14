import socket
import struct
import time
from collections import deque
from threading import Lock

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from devices.camera.preview_config import PREVIEW_MAX_JPEG_BYTES


class CameraPreviewReceiver(QThread):
    connected = Signal()
    frame_received = Signal()
    failed = Signal(str)

    def __init__(self, host, port, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self._socket = None
        self._stopping = False
        self._frame_lock = Lock()
        self._latest_frame = None
        self._notification_pending = False

    def take_latest_frame(self):
        with self._frame_lock:
            frame = self._latest_frame
            self._latest_frame = None
            self._notification_pending = False
        return frame

    def stop(self):
        self._stopping = True
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def run(self):
        frame_times = deque()
        received = 0
        try:
            deadline = time.monotonic() + 5.0
            last_error = None
            sock = None
            while not self._stopping and time.monotonic() < deadline:
                try:
                    sock = socket.create_connection((self.host, self.port), timeout=1.0)
                    break
                except OSError as exc:
                    last_error = exc
                    time.sleep(0.2)
            if sock is None:
                raise ConnectionError(f"Preview connection failed: {last_error}")
            self._socket = sock
            sock.settimeout(2.0)
            self.connected.emit()
            while not self._stopping:
                header = self._read_exact(sock, 4)
                length = struct.unpack("!I", header)[0]
                if length <= 0 or length > PREVIEW_MAX_JPEG_BYTES:
                    raise ValueError(f"Invalid preview JPEG length: {length}")
                payload = self._read_exact(sock, length)
                image = QImage.fromData(payload, "JPEG")
                if image.isNull():
                    raise ValueError("Preview JPEG decode failed.")
                received += 1
                current = time.monotonic()
                frame_times.append(current)
                while frame_times and current - frame_times[0] > 1.0:
                    frame_times.popleft()
                fps = 0.0
                if len(frame_times) > 1:
                    fps = (len(frame_times) - 1) / (frame_times[-1] - frame_times[0])
                notify = False
                with self._frame_lock:
                    self._latest_frame = (image, fps, len(payload), received)
                    if not self._notification_pending:
                        self._notification_pending = True
                        notify = True
                if notify:
                    self.frame_received.emit()
        except Exception as exc:
            if not self._stopping:
                self.failed.emit(str(exc))
        finally:
            sock = self._socket
            self._socket = None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _read_exact(self, sock, size):
        chunks = bytearray()
        while len(chunks) < size and not self._stopping:
            chunk = sock.recv(size - len(chunks))
            if not chunk:
                raise ConnectionError("Preview connection closed.")
            chunks.extend(chunk)
        if len(chunks) != size:
            raise ConnectionError("Preview receiver stopped.")
        return bytes(chunks)
