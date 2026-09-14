from __future__ import annotations

import os
from collections import deque
from pathlib import Path


class IncrementalLogTail:
    def __init__(self, path: str | Path | None = None, *, initial_lines: int = 500):
        self.path = Path(path) if path else None
        self.initial_lines = max(1, int(initial_lines))
        self.offset = 0
        self.identity: tuple[int, int] | None = None

    def select(self, path: str | Path, *, initial_lines: int | None = None) -> str:
        self.path = Path(path)
        if initial_lines is not None:
            self.initial_lines = max(1, int(initial_lines))
        self.offset = 0
        self.identity = None
        if not self.path.is_file():
            return "[Log file is not available yet.]"
        stat = self.path.stat()
        self.identity = (stat.st_dev, stat.st_ino)
        self.offset = stat.st_size
        return self._read_last_lines(self.path, self.initial_lines)

    def read_new(self) -> str:
        if self.path is None:
            return ""
        try:
            stat = self.path.stat()
        except OSError:
            return ""
        identity = (stat.st_dev, stat.st_ino)
        reset = self.identity is not None and (identity != self.identity or stat.st_size < self.offset)
        if self.identity is None or reset:
            self.offset = 0
            self.identity = identity
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                data = stream.read()
                self.offset = stream.tell()
        except OSError:
            return ""
        if not data:
            return ""
        prefix = "[Log file was truncated or replaced.]\n" if reset else ""
        return prefix + data.decode("utf-8", errors="replace")

    @staticmethod
    def _read_last_lines(path: Path, line_count: int) -> str:
        chunks: deque[bytes] = deque()
        newlines = 0
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            while position > 0 and newlines <= line_count:
                size = min(65536, position)
                position -= size
                stream.seek(position)
                chunk = stream.read(size)
                chunks.appendleft(chunk)
                newlines += chunk.count(b"\n")
        data = b"".join(chunks)
        lines = data.splitlines(keepends=True)
        return b"".join(lines[-line_count:]).decode("utf-8", errors="replace")
