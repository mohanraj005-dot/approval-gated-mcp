"""The approval gate: draft, preview, approve (human side) and send_draft (agent side)."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Iterable
from typing import Any

from .audit import AuditLog
from .config import Config
from .errors import DraftError, SendDenied, SendOutcomeUnknown
from .models import (
    Attachment,
    Draft,
    normalize_addresses,
    normalize_body,
    normalize_subject,
)
from .models import fingerprint as fp_of
from .store import Store
from .transport import Transport, build_message

Approver = Callable[[dict[str, Any]], bool]  # receives the full preview, returns a decision
Addrs = str | Iterable[str] | None


class ApprovalGate:
    def __init__(
        self,
        config: Config,
        transport: Transport,
        *,
        approver: Approver | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.transport = transport
        self.approver = approver  # host-supplied; never reachable from an agent tool
        self.clock = clock
        self.store = Store(config.state_dir)
        self.audit = AuditLog(config.state_dir / "audit.jsonl", clock)

    # ---- agent-facing -------------------------------------------------------------

    def draft(
        self,
        to: Addrs,
        subject: str,
        body: str,
        cc: Addrs = None,
        bcc: Addrs = None,
        attachments: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        now = self.clock()
        draft = self._build(
            self.store.new_draft_id(),
            now,
            now,
            to,
            subject,
            body,
            cc,
            bcc,
            self._ingest(attachments or ()),
        )
        self.store.save_draft(draft)
        self.audit.write("draft", actor="agent", **self._summary(draft))
        return {"draft_id": draft.draft_id, "fingerprint": fp_of(draft), "state": "pending"}

    def update_draft(
        self,
        draft_id: str,
        *,
        to: Addrs = None,
        subject: str | None = None,
        body: str | None = None,
        cc: Addrs = None,
        bcc: Addrs = None,
        attachments: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Edit a draft. The fingerprint changes, so any earlier approval stops matching."""
        with self.store.lock():
            old = self.store.load_draft(draft_id)
            if self.store.load_claim(draft_id) is not None:
                raise DraftError("draft already claimed for sending; it can no longer be edited")
            new = self._build(
                old.draft_id,
                old.created_at,
                self.clock(),
                old.to if to is None else to,
                old.subject if subject is None else subject,
                old.body if body is None else body,
                old.cc if cc is None else cc,
                old.bcc if bcc is None else bcc,
                old.attachments if attachments is None else self._ingest(attachments),
            )
            self.store.save_draft(new)
            self.audit.write(
                "update", actor="agent", previous_fingerprint=fp_of(old), **self._summary(new)
            )
        return {"draft_id": new.draft_id, "fingerprint": fp_of(new), "state": "pending"}

    def draft_preview(self, draft_id: str, *, full: bool = False) -> dict[str, Any]:
        draft = self.store.load_draft(draft_id)
        fp = fp_of(draft)
        excerpt = draft.body if full else draft.body[: self.config.excerpt_chars]
        return {
            "draft_id": draft.draft_id,
            "from": draft.from_addr,
            "to": list(draft.to),
            "cc": list(draft.cc),
            "bcc": list(draft.bcc),
            "subject": draft.subject,
            "attachments": [
                {"name": a.name, "size": a.size, "sha256": a.sha256} for a in draft.attachments
            ],
            "body_excerpt": excerpt,
            "body_chars": len(draft.body),
            "body_truncated": len(excerpt) < len(draft.body),
            "fingerprint": fp,
            "state": self._state(draft, fp),
            "sending_enabled": self.config.kill_switch_reason() is None,
        }

    def send_draft(self, draft_id: str, fingerprint: str) -> dict[str, Any]:
        try:
            return self._send(draft_id, fingerprint)
        except SendDenied as exc:
            self.audit.write(
                "send_denied",
                actor="agent",
                draft_id=draft_id,
                fingerprint=fingerprint,
                reason=exc.reason,
            )
            raise

    # ---- human-facing (deliberately not exposed as an MCP tool) -------------------

    def approve(self, draft_id: str, fingerprint: str, approver: str = "human") -> dict[str, Any]:
        draft = self.store.load_draft(draft_id)
        current = fp_of(draft)
        if not hmac.compare_digest(current, str(fingerprint)):
            self.audit.write(
                "approve_denied",
                actor=approver,
                draft_id=draft_id,
                fingerprint=fingerprint,
                reason="stale_fingerprint",
                current=current,
            )
            raise SendDenied(
                "stale_fingerprint", f"draft changed; current fingerprint is {current}"
            )
        record = {
            "draft_id": draft_id,
            "fingerprint": current,
            "approved_at": self.clock(),
            "approver": approver,
        }
        self.store.save_approval(draft_id, current, record)
        self.audit.write("approve", actor=approver, draft_id=draft_id, fingerprint=current)
        return record

    # ---- internals ----------------------------------------------------------------

    def _send(self, draft_id: str, supplied: str) -> dict[str, Any]:
        off = self.config.kill_switch_reason()
        if off:
            raise SendDenied("sending_disabled", off)
        with self.store.lock():
            try:
                draft = self.store.load_draft(draft_id)
            except DraftError as exc:
                raise SendDenied("unknown_draft", str(exc)) from None
            existing = self.store.load_claim(draft_id)
            if existing is not None:
                if existing.get("state") == "sent":
                    raise SendDenied("already_sent", "this draft was already sent; it sends once")
                raise SendDenied(
                    "outcome_unknown",
                    "an earlier send did not complete cleanly and is never retried "
                    "automatically; check the destination, then create a new draft if needed",
                )
            current = fp_of(draft)
            if not hmac.compare_digest(current, str(supplied)):
                raise SendDenied(
                    "stale_fingerprint",
                    "fingerprint does not match the current draft; preview it again and "
                    "ask the human to re-approve",
                )
            self._require_approval(draft, current)
            # Build (and integrity-check attachments) before consuming the claim.
            msg = build_message(draft, self._load_blobs(draft))
            now = self.clock()
            recent = [t for t in self.store.claim_times() if now - t < self.config.rate_window_s]
            if len(recent) >= self.config.rate_limit:
                raise SendDenied(
                    "rate_limited",
                    f"limit of {self.config.rate_limit} sends per "
                    f"{self.config.rate_window_s // 60} minutes reached",
                )
            claim = {"state": "sending", "claimed_at": now, "fingerprint": current}
            if not self.store.try_claim(draft_id, claim):  # exclusive create: at most once
                raise SendDenied("already_sent", "send already claimed")
            self.audit.write("send_claimed", actor="agent", draft_id=draft_id, fingerprint=current)

        # The transport runs outside the lock. A crash from here on leaves state "sending",
        # which later calls treat as outcome_unknown.
        try:
            ref = self.transport.send(msg, draft.recipients)
        except Exception as exc:
            err = type(exc).__name__
            self.store.update_claim(draft_id, {**claim, "state": "unknown", "error": err})
            self.audit.write(
                "send_unknown", actor="agent", draft_id=draft_id, fingerprint=current, error=err
            )
            raise SendOutcomeUnknown(
                f"transport failed ({err}); the message may or may not have been delivered. "
                "It will not be retried automatically. Do not call send_draft again"
            ) from exc
        self.store.update_claim(draft_id, {**claim, "state": "sent", "ref": ref})
        self.audit.write(
            "send",
            actor="agent",
            draft_id=draft_id,
            fingerprint=current,
            ref=ref,
            to=list(draft.to),
            cc=list(draft.cc),
            bcc=len(draft.bcc),
        )
        return {"status": "sent", "draft_id": draft_id, "fingerprint": current, "ref": ref}

    def _require_approval(self, draft: Draft, fp: str) -> None:
        approval = self.store.load_approval(draft.draft_id, fp)
        if approval is None and self.approver is not None:
            # The callback sees the full preview. It runs inside the store lock, so a slow
            # human prompt blocks other sends; fine for a reference implementation.
            if self.approver(self.draft_preview(draft.draft_id, full=True)):
                approval = self.approve(draft.draft_id, fp, approver="callback")
            else:
                self.audit.write(
                    "approve_denied",
                    actor="callback",
                    draft_id=draft.draft_id,
                    fingerprint=fp,
                    reason="declined",
                )
        if approval is None:
            raise SendDenied("not_approved", "no human approval for this exact draft content")
        if not self._fresh(approval):
            raise SendDenied("approval_expired", "the approval is older than the allowed window")

    def _state(self, draft: Draft, fp: str) -> str:
        claim = self.store.load_claim(draft.draft_id)
        if claim is not None:
            return "sent" if claim.get("state") == "sent" else "outcome_unknown"
        approval = self.store.load_approval(draft.draft_id, fp)
        if approval and self._fresh(approval):
            return "approved"
        return "pending"

    def _fresh(self, approval: dict[str, Any]) -> bool:
        return self.clock() - float(approval["approved_at"]) <= self.config.approval_ttl_s

    def _load_blobs(self, draft: Draft) -> dict[str, bytes]:
        blobs: dict[str, bytes] = {}
        for att in draft.attachments:
            data = self.store.load_blob(att.sha256)
            if hashlib.sha256(data).hexdigest() != att.sha256:
                raise SendDenied("attachment_integrity", f"stored bytes for {att.name} changed")
            blobs[att.sha256] = data
        return blobs

    def _summary(self, d: Draft) -> dict[str, Any]:
        return {
            "draft_id": d.draft_id,
            "fingerprint": fp_of(d),
            "from": d.from_addr,
            "to": list(d.to),
            "cc": list(d.cc),
            "bcc": len(d.bcc),
            "subject": d.subject,
            "attachments": [a.name for a in d.attachments],
        }

    def _build(
        self,
        draft_id: str,
        created: float,
        updated: float,
        to: Addrs,
        subject: str,
        body: str,
        cc: Addrs,
        bcc: Addrs,
        attachments: tuple[Attachment, ...],
    ) -> Draft:
        cfg = self.config
        to_t = normalize_addresses(to)
        cc_t = normalize_addresses(cc)
        bcc_t = normalize_addresses(bcc)
        everyone = {*to_t, *cc_t, *bcc_t}
        if not to_t:
            raise DraftError("at least one 'to' recipient is required")
        if len(everyone) > cfg.max_recipients:
            raise DraftError(f"too many recipients (max {cfg.max_recipients})")
        if cfg.allowed_domains is not None:
            bad = sorted(a for a in everyone if a.split("@")[1] not in cfg.allowed_domains)
            if bad:
                raise DraftError(f"recipient domain not allowed: {', '.join(bad)}")
        return Draft(
            draft_id=draft_id,
            from_addr=cfg.from_addr.strip().lower(),  # fixed by config, never agent-supplied
            to=to_t,
            cc=cc_t,
            bcc=bcc_t,
            subject=normalize_subject(subject),
            body=normalize_body(body, cfg.max_body_chars),
            attachments=attachments,
            created_at=created,
            updated_at=updated,
        )

    def _ingest(self, paths: Iterable[str]) -> tuple[Attachment, ...]:
        """Copy each file into the store and pin its hash. Only files inside attach_dir."""
        names = list(paths)
        if not names:
            return ()
        root = self.config.attach_dir
        if root is None:
            raise DraftError("attachments are disabled (set AGM_ATTACH_DIR to enable)")
        if len(names) > self.config.max_attachments:
            raise DraftError(f"too many attachments (max {self.config.max_attachments})")
        root = root.resolve()
        out: list[Attachment] = []
        for raw in names:
            path = (root / str(raw)).resolve()  # resolves symlinks and ".." before the check
            if not path.is_relative_to(root) or not path.is_file():
                raise DraftError(f"attachment must be a file inside the attachment dir: {raw!r}")
            data = path.read_bytes()
            if len(data) > self.config.max_attachment_bytes:
                raise DraftError(f"attachment too large: {path.name}")
            digest = hashlib.sha256(data).hexdigest()
            self.store.save_blob(digest, data)
            out.append(Attachment(name=path.name, sha256=digest, size=len(data)))
        if len({a.name for a in out}) != len(out):
            raise DraftError("attachment names must be unique")
        return tuple(sorted(out, key=lambda a: a.name))
