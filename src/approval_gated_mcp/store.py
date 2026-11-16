"""File-backed state shared by the server process and the approval CLI.

Layout under the state directory:
  drafts/<id>.json              current content of each draft
  blobs/<sha256>                attachment bytes, pinned at draft time
  approvals/<id>.<fp>.json      one human approval per (draft, fingerprint)
  sends/<id>.json               exclusive send claim: sending | sent | unknown
  audit.jsonl                   append-only event log
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .errors import DraftError
from .models import Draft

DRAFT_ID_RE = re.compile(r"^d_[0-9a-f]{12}$")
FP_RE = re.compile(r"^[0-9a-f]{24}$")


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        for sub in ("drafts", "blobs", "approvals", "sends"):
            (root / sub).mkdir(parents=True, exist_ok=True, mode=0o700)

    # ids are validated before they ever touch a path, so a draft_id cannot traverse
    def _id(self, draft_id: str) -> str:
        if not isinstance(draft_id, str) or not DRAFT_ID_RE.match(draft_id):
            raise DraftError(f"unknown draft: {draft_id!r}")
        return draft_id

    @staticmethod
    def new_draft_id() -> str:
        return "d_" + secrets.token_hex(6)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        with open(self.root / ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # drafts
    def save_draft(self, draft: Draft) -> None:
        path = self.root / "drafts" / f"{self._id(draft.draft_id)}.json"
        _atomic_write(path, json.dumps(draft.to_json(), sort_keys=True).encode())

    def load_draft(self, draft_id: str) -> Draft:
        path = self.root / "drafts" / f"{self._id(draft_id)}.json"
        try:
            return Draft.from_json(json.loads(path.read_text()))
        except FileNotFoundError:
            raise DraftError(f"unknown draft: {draft_id!r}") from None

    def list_drafts(self) -> list[Draft]:
        paths = sorted((self.root / "drafts").glob("d_*.json"))
        return [Draft.from_json(json.loads(p.read_text())) for p in paths]

    # attachment blobs
    def save_blob(self, sha256: str, data: bytes) -> None:
        path = self.root / "blobs" / sha256
        if not path.exists():
            _atomic_write(path, data)

    def load_blob(self, sha256: str) -> bytes:
        return (self.root / "blobs" / sha256).read_bytes()

    # approvals
    def save_approval(self, draft_id: str, fp: str, record: dict[str, Any]) -> None:
        path = self.root / "approvals" / f"{self._id(draft_id)}.{fp}.json"
        _atomic_write(path, json.dumps(record, sort_keys=True).encode())

    def load_approval(self, draft_id: str, fp: str) -> dict[str, Any] | None:
        if not FP_RE.match(fp):
            return None
        path = self.root / "approvals" / f"{self._id(draft_id)}.{fp}.json"
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None

    # send claims: exclusive creation is what makes a send happen at most once
    def try_claim(self, draft_id: str, record: dict[str, Any]) -> bool:
        path = self.root / "sends" / f"{self._id(draft_id)}.json"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        try:
            os.write(fd, json.dumps(record, sort_keys=True).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return True

    def load_claim(self, draft_id: str) -> dict[str, Any] | None:
        path = self.root / "sends" / f"{self._id(draft_id)}.json"
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            return {"state": "sending"}  # claim exists but is mid-write: treat as in flight

    def update_claim(self, draft_id: str, record: dict[str, Any]) -> None:
        path = self.root / "sends" / f"{self._id(draft_id)}.json"
        _atomic_write(path, json.dumps(record, sort_keys=True).encode())

    def claim_times(self) -> list[float]:
        times: list[float] = []
        for p in (self.root / "sends").glob("d_*.json"):
            with contextlib.suppress(ValueError, OSError):
                times.append(float(json.loads(p.read_text()).get("claimed_at", 0)))
        return times
