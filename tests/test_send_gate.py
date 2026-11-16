from __future__ import annotations

import threading
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from approval_gated_mcp import ApprovalGate, Config, DraftError, SendDenied, SendOutcomeUnknown

from .conftest import FakeClock, RecordingTransport, approved_draft, make_draft


def events(gate: ApprovalGate) -> list[str]:
    return [e["event"] for e in gate.audit.read()]


def test_send_denied_without_approval(gate: ApprovalGate, transport: RecordingTransport) -> None:
    d = make_draft(gate)
    with pytest.raises(SendDenied) as exc:
        gate.send_draft(d["draft_id"], d["fingerprint"])
    assert exc.value.reason == "not_approved"
    assert transport.sent == []


def test_send_denied_with_wrong_fingerprint(gate: ApprovalGate, transport) -> None:
    d = approved_draft(gate)
    with pytest.raises(SendDenied) as exc:
        gate.send_draft(d["draft_id"], "0" * 24)
    assert exc.value.reason == "stale_fingerprint"
    assert transport.sent == []


def test_unknown_and_malformed_draft_ids_are_denied(gate: ApprovalGate) -> None:
    for bad in ("d_000000000000", "../../etc/passwd", ""):
        with pytest.raises(SendDenied) as exc:
            gate.send_draft(bad, "0" * 24)
        assert exc.value.reason == "unknown_draft"


def test_approved_send_goes_out_once(gate: ApprovalGate, transport: RecordingTransport) -> None:
    d = approved_draft(gate, cc=["carol@example.org"], bcc=["dan@example.org"])
    result = gate.send_draft(d["draft_id"], d["fingerprint"])
    assert result["status"] == "sent"
    ((msg, rcpts),) = transport.sent
    assert sorted(rcpts) == ["alice@example.org", "carol@example.org", "dan@example.org"]
    assert msg["From"] == "agent@example.com"
    assert "dan@example.org" not in str(msg)  # bcc never appears in headers
    assert msg["X-AGM-Fingerprint"] == d["fingerprint"]
    assert gate.draft_preview(d["draft_id"])["state"] == "sent"


def test_double_send_is_prevented(gate: ApprovalGate, transport: RecordingTransport) -> None:
    d = approved_draft(gate)
    gate.send_draft(d["draft_id"], d["fingerprint"])
    with pytest.raises(SendDenied) as exc:
        gate.send_draft(d["draft_id"], d["fingerprint"])
    assert exc.value.reason == "already_sent"
    assert len(transport.sent) == 1
    with pytest.raises(DraftError):  # a sent draft cannot be edited back into an approved shape
        gate.update_draft(d["draft_id"], body="new text")


def test_transport_failure_records_unknown_outcome_and_never_retries(
    gate: ApprovalGate, transport: RecordingTransport
) -> None:
    d = approved_draft(gate)
    transport.fail_with = TimeoutError("connection dropped after DATA")
    with pytest.raises(SendOutcomeUnknown):
        gate.send_draft(d["draft_id"], d["fingerprint"])
    assert gate.draft_preview(d["draft_id"])["state"] == "outcome_unknown"

    transport.fail_with = None  # even if the transport recovers, the gate does not retry
    with pytest.raises(SendDenied) as exc:
        gate.send_draft(d["draft_id"], d["fingerprint"])
    assert exc.value.reason == "outcome_unknown"
    assert transport.sent == []
    assert "send_unknown" in events(gate)


def test_kill_switch_env_blocks_every_send(
    gate: ApprovalGate, transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = approved_draft(gate)
    monkeypatch.setenv("AGM_SENDING", "off")
    with pytest.raises(SendDenied) as exc:
        gate.send_draft(d["draft_id"], d["fingerprint"])
    assert exc.value.reason == "sending_disabled"
    assert gate.draft_preview(d["draft_id"])["sending_enabled"] is False
    monkeypatch.setenv("AGM_SENDING", "on")  # the draft was not consumed by the denial
    assert gate.send_draft(d["draft_id"], d["fingerprint"])["status"] == "sent"


def test_kill_switch_file_blocks_every_send(gate: ApprovalGate, transport) -> None:
    d = approved_draft(gate)
    kill = gate.config.state_dir / "SENDING_OFF"
    kill.touch()
    with pytest.raises(SendDenied) as exc:
        gate.send_draft(d["draft_id"], d["fingerprint"])
    assert exc.value.reason == "sending_disabled"
    kill.unlink()
    assert gate.send_draft(d["draft_id"], d["fingerprint"])["status"] == "sent"
    assert len(transport.sent) == 1


def test_rate_limit_and_window(gate: ApprovalGate, transport, clock: FakeClock) -> None:
    drafts = [approved_draft(gate, subject=f"Mail {i}") for i in range(5)]
    for d in drafts[:3]:  # config.rate_limit == 3
        gate.send_draft(d["draft_id"], d["fingerprint"])
    with pytest.raises(SendDenied) as exc:
        gate.send_draft(drafts[3]["draft_id"], drafts[3]["fingerprint"])
    assert exc.value.reason == "rate_limited"
    clock.advance(3601)  # window rolls over
    gate.approve(drafts[3]["draft_id"], drafts[3]["fingerprint"])  # approval is still fresh
    assert gate.send_draft(drafts[3]["draft_id"], drafts[3]["fingerprint"])["status"] == "sent"
    assert len(transport.sent) == 4


def test_approval_expires(gate: ApprovalGate, clock: FakeClock, config: Config) -> None:
    d = approved_draft(gate)
    clock.advance(config.approval_ttl_s + 1)
    with pytest.raises(SendDenied) as exc:
        gate.send_draft(d["draft_id"], d["fingerprint"])
    assert exc.value.reason == "approval_expired"


def test_injected_approver_callback(config: Config, transport, clock: FakeClock) -> None:
    seen: list[dict] = []

    def approver(preview: dict) -> bool:
        seen.append(preview)
        return preview["subject"] == "Hello"

    gate = ApprovalGate(config, transport, approver=approver, clock=clock)
    ok = make_draft(gate)
    assert gate.send_draft(ok["draft_id"], ok["fingerprint"])["status"] == "sent"
    assert seen[0]["body_excerpt"].startswith("Hi Alice")  # callback sees the full preview

    refused = make_draft(gate, subject="Something else")
    with pytest.raises(SendDenied) as exc:
        gate.send_draft(refused["draft_id"], refused["fingerprint"])
    assert exc.value.reason == "not_approved"
    assert len(transport.sent) == 1


def test_audit_log_records_every_stage(gate: ApprovalGate) -> None:
    d = make_draft(gate)
    with pytest.raises(SendDenied):
        gate.send_draft(d["draft_id"], d["fingerprint"])
    gate.approve(d["draft_id"], d["fingerprint"], approver="cli")
    gate.send_draft(d["draft_id"], d["fingerprint"])
    with pytest.raises(SendDenied):
        gate.send_draft(d["draft_id"], d["fingerprint"])

    rows = gate.audit.read()
    assert events(gate) == [
        "draft",
        "send_denied",
        "approve",
        "send_claimed",
        "send",
        "send_denied",
    ]
    assert [r.get("reason") for r in rows if r["event"] == "send_denied"] == [
        "not_approved",
        "already_sent",
    ]
    assert all(r["draft_id"] == d["draft_id"] for r in rows)
    assert rows[2]["actor"] == "cli"
    assert all("body" not in r and "Hi Alice" not in str(r) for r in rows)  # no content in logs
    raw = gate.audit.path.read_text().splitlines()
    assert len(raw) == len(rows)  # one JSON object per line


def test_recipient_allowlist(config: Config, transport) -> None:
    from dataclasses import replace

    gate = ApprovalGate(replace(config, allowed_domains=frozenset({"example.org"})), transport)
    make_draft(gate)
    with pytest.raises(DraftError):
        make_draft(gate, to=["someone@elsewhere.com"])


def test_attachments_pinned_and_confined(gate: ApprovalGate, file_gate: ApprovalGate) -> None:
    root = file_gate.config.attach_dir
    (root / "report.txt").write_text("v1")
    d = make_draft(file_gate, attachments=["report.txt"])
    (root / "report.txt").write_text("v2 swapped after drafting")  # TOCTOU attempt
    file_gate.approve(d["draft_id"], d["fingerprint"])
    result = file_gate.send_draft(d["draft_id"], d["fingerprint"])
    parsed = BytesParser(policy=policy.default).parsebytes(Path(result["ref"]).read_bytes())
    (att,) = list(parsed.iter_attachments())
    assert att.get_filename() == "report.txt"
    assert att.get_content() == b"v1"  # the bytes pinned at draft time, not the swapped file
    preview = file_gate.draft_preview(d["draft_id"])
    assert preview["attachments"][0]["name"] == "report.txt"
    assert preview["attachments"][0]["size"] == 2

    outside = root.parent / "secret.txt"
    outside.write_text("nope")
    for bad in ("../secret.txt", str(outside)):
        with pytest.raises(DraftError):
            make_draft(file_gate, attachments=[bad])


def test_attachments_disabled_by_default(config: Config, transport) -> None:
    from dataclasses import replace

    gate = ApprovalGate(replace(config, attach_dir=None), transport)
    with pytest.raises(DraftError):
        make_draft(gate, attachments=["x.txt"])


def test_file_outbox_writes_eml_and_envelope(file_gate: ApprovalGate) -> None:
    d = approved_draft(file_gate, bcc=["dan@example.org"])
    result = file_gate.send_draft(d["draft_id"], d["fingerprint"])
    eml = file_gate.config.outbox_dir / f"{d['draft_id']}.eml"
    assert result["ref"] == str(eml) and eml.exists()
    assert "Subject: Hello" in eml.read_text()
    assert "dan@example.org" in (eml.parent / f"{d['draft_id']}.envelope.json").read_text()


def test_concurrent_sends_of_one_draft_send_once(gate: ApprovalGate, transport) -> None:
    d = approved_draft(gate)
    outcomes: list[str] = []

    def attempt() -> None:
        try:
            gate.send_draft(d["draft_id"], d["fingerprint"])
            outcomes.append("sent")
        except SendDenied as exc:
            outcomes.append(exc.reason)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("sent") == 1
    assert len(transport.sent) == 1
