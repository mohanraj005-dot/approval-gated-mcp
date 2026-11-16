"""Pluggable transports. The gate only ever calls `Transport.send`."""

from __future__ import annotations

import json
import os
import smtplib
import ssl
from collections.abc import Mapping, Sequence
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Protocol

from .config import Config
from .models import Draft, fingerprint


class Transport(Protocol):
    def send(self, message: EmailMessage, recipients: Sequence[str]) -> str:
        """Deliver `message` to the envelope `recipients`. Return a reference (path or
        message id). Raise on failure; the gate treats any exception as outcome unknown."""
        ...


def build_message(draft: Draft, blobs: Mapping[str, bytes]) -> EmailMessage:
    """Build the RFC 5322 message. Bcc recipients are deliberately not put in any header."""
    msg = EmailMessage()
    msg["From"] = draft.from_addr
    msg["To"] = ", ".join(draft.to)
    if draft.cc:
        msg["Cc"] = ", ".join(draft.cc)
    msg["Subject"] = draft.subject
    msg["Date"] = formatdate(draft.updated_at, localtime=False, usegmt=True)
    domain = draft.from_addr.split("@", 1)[1]
    msg["Message-ID"] = f"<{draft.draft_id}@{domain}>"
    msg["X-AGM-Draft-Id"] = draft.draft_id
    msg["X-AGM-Fingerprint"] = fingerprint(draft)
    msg.set_content(draft.body)
    for att in draft.attachments:
        msg.add_attachment(
            blobs[att.sha256], maintype="application", subtype="octet-stream", filename=att.name
        )
    return msg


class FileOutbox:
    """Default transport: writes <draft_id>.eml and <draft_id>.envelope.json to a directory.
    No network, no credentials. Refuses to overwrite an existing file."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def send(self, message: EmailMessage, recipients: Sequence[str]) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = message["X-AGM-Draft-Id"]
        eml = self.directory / f"{stem}.eml"
        fd = os.open(eml, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(bytes(message))
        envelope = self.directory / f"{stem}.envelope.json"
        envelope.write_text(json.dumps({"recipients": list(recipients)}, indent=2))
        return str(eml)


class SmtpTransport:
    """SMTP backend. Off by default and NOT tested against a real server; shipped to show
    the seam, not as a hardened mailer. Uses STARTTLS with certificate verification."""

    def __init__(
        self,
        host: str,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.username, self.password, self.starttls = username, password, starttls

    def send(self, message: EmailMessage, recipients: Sequence[str]) -> str:
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self.username and self.password:
                smtp.login(self.username, self.password)
            smtp.send_message(message, to_addrs=list(recipients))
        return str(message["Message-ID"])


def build_transport(config: Config, env: Mapping[str, str] | None = None) -> Transport:
    e = os.environ if env is None else env
    if e.get("AGM_TRANSPORT", "file").lower() == "smtp":
        return SmtpTransport(
            host=e["AGM_SMTP_HOST"],
            port=int(e.get("AGM_SMTP_PORT", "587")),
            username=e.get("AGM_SMTP_USER"),
            password=e.get("AGM_SMTP_PASSWORD"),
        )
    return FileOutbox(config.outbox_dir)
