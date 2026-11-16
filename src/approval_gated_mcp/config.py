"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

OFF_VALUES = frozenset({"off", "0", "false", "no", "disabled"})
KILL_FILE = "SENDING_OFF"


@dataclass(frozen=True)
class Config:
    state_dir: Path = Path(".agm")
    outbox_dir: Path = Path("outbox")
    from_addr: str = "agent@example.com"
    rate_limit: int = 10  # sends per rate_window_s
    rate_window_s: int = 3600
    approval_ttl_s: int = 86400
    attach_dir: Path | None = None  # attachments are refused unless this is set
    allowed_domains: frozenset[str] | None = None  # None means any recipient domain
    max_recipients: int = 20
    max_body_chars: int = 100_000
    max_attachments: int = 5
    max_attachment_bytes: int = 5_000_000
    excerpt_chars: int = 500

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = os.environ if env is None else env
        domains = e.get("AGM_ALLOWED_DOMAINS", "").strip()
        attach = e.get("AGM_ATTACH_DIR", "").strip()
        return cls(
            state_dir=Path(e.get("AGM_HOME", ".agm")),
            outbox_dir=Path(e.get("AGM_OUTBOX", "outbox")),
            from_addr=e.get("AGM_FROM", cls.from_addr),
            rate_limit=int(e.get("AGM_RATE_LIMIT", cls.rate_limit)),
            approval_ttl_s=int(e.get("AGM_APPROVAL_TTL", cls.approval_ttl_s)),
            attach_dir=Path(attach) if attach else None,
            allowed_domains=(
                frozenset(d.strip().lower() for d in domains.split(",") if d.strip())
                if domains
                else None
            ),
        )

    def kill_switch_reason(self, env: dict[str, str] | None = None) -> str | None:
        """Return why sending is disabled, or None if it is enabled. Read on every call
        so flipping the switch takes effect immediately, without a restart."""
        e = os.environ if env is None else env
        if e.get("AGM_SENDING", "on").strip().lower() in OFF_VALUES:
            return "AGM_SENDING is off"
        if (self.state_dir / KILL_FILE).exists():
            return f"kill-switch file {self.state_dir / KILL_FILE} exists"
        return None
