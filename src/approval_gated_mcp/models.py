"""Draft model, input normalisation and the fingerprint.

The fingerprint is computed over the *stored, already-normalised* draft, so what a human
approves is byte-for-byte what the transport later sends.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .errors import DraftError

FP_VERSION = "agm-fp-v1"
FP_LENGTH = 24  # hex chars = 96 bits; long enough that grinding a collision is impractical
ADDR_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # all C0 controls except \t \n \r
MAX_SUBJECT = 200


@dataclass(frozen=True)
class Attachment:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Draft:
    draft_id: str
    from_addr: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    attachments: tuple[Attachment, ...]
    created_at: float
    updated_at: float

    @property
    def recipients(self) -> list[str]:
        return list(dict.fromkeys([*self.to, *self.cc, *self.bcc]))

    def to_json(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["attachments"] = [a.__dict__ for a in self.attachments]
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Draft:
        return cls(
            draft_id=d["draft_id"],
            from_addr=d["from_addr"],
            to=tuple(d["to"]),
            cc=tuple(d["cc"]),
            bcc=tuple(d["bcc"]),
            subject=d["subject"],
            body=d["body"],
            attachments=tuple(Attachment(**a) for a in d["attachments"]),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
        )


def normalize_address(raw: str) -> str:
    addr = unicodedata.normalize("NFC", str(raw)).strip().lower()
    if not ADDR_RE.match(addr):
        # Display names, angle brackets, commas and non-ASCII (IDN) addresses are refused on
        # purpose: what the human reads must be exactly what the envelope uses.
        raise DraftError(f"invalid address: {raw!r} (plain ASCII addr@domain only)")
    return addr


def normalize_addresses(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    items = [raw] if isinstance(raw, str) else list(raw)
    return tuple(sorted({normalize_address(a) for a in items}))


def normalize_subject(raw: str) -> str:
    subject = unicodedata.normalize("NFC", str(raw)).strip()
    if not subject or len(subject) > MAX_SUBJECT:
        raise DraftError(f"subject must be 1-{MAX_SUBJECT} characters")
    if "\n" in subject or "\r" in subject or CONTROL_RE.search(subject):
        raise DraftError("subject must be a single line without control characters")
    return subject


def normalize_body(raw: str, max_chars: int) -> str:
    body = unicodedata.normalize("NFC", str(raw)).replace("\r\n", "\n").replace("\r", "\n").rstrip()
    if not body or len(body) > max_chars:
        raise DraftError(f"body must be 1-{max_chars} characters")
    if CONTROL_RE.search(body):
        raise DraftError("body contains control characters")
    return body


def canonical_bytes(d: Draft) -> bytes:
    payload = {
        "v": FP_VERSION,
        "from": d.from_addr,
        "to": sorted(d.to),
        "cc": sorted(d.cc),
        "bcc": sorted(d.bcc),
        "subject": d.subject,
        "body": d.body,
        "attachments": sorted([a.name, a.sha256] for a in d.attachments),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def fingerprint(d: Draft) -> str:
    return hashlib.sha256(canonical_bytes(d)).hexdigest()[:FP_LENGTH]
