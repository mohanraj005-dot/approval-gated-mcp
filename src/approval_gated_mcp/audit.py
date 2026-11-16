"""Append-only JSONL audit log.

One event per line, written with a single O_APPEND write so concurrent writers do not
interleave. Message bodies are never logged. This is append-only by convention: a process
with write access to the file can still rewrite it (see README, limitations).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": round(self.clock(), 3), "event": event, **fields}
        line = (json.dumps(record, sort_keys=True) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def read(self, last: int | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]
        return rows[-last:] if last else rows
