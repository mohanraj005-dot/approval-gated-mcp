from __future__ import annotations

import dataclasses

import pytest

from approval_gated_mcp import ApprovalGate, DraftError, SendDenied, fingerprint
from approval_gated_mcp.models import FP_LENGTH, Attachment

from .conftest import approved_draft, make_draft


def load(gate: ApprovalGate, draft_id: str):
    return gate.store.load_draft(draft_id)


def test_fingerprint_is_stable_and_short(gate: ApprovalGate) -> None:
    d = make_draft(gate)
    draft = load(gate, d["draft_id"])
    assert fingerprint(draft) == fingerprint(draft) == d["fingerprint"]
    assert len(d["fingerprint"]) == FP_LENGTH
    assert gate.draft_preview(d["draft_id"])["fingerprint"] == d["fingerprint"]


def test_same_content_same_fingerprint_regardless_of_cosmetics(gate: ApprovalGate) -> None:
    a = make_draft(
        gate, to=["Bob@Example.org", "alice@example.org"], body="Line one\r\nLine two  \n"
    )
    b = make_draft(gate, to=["alice@example.org", "bob@example.org", "bob@example.org"],
                   body="Line one\nLine two")  # fmt: skip
    assert a["draft_id"] != b["draft_id"]
    assert a["fingerprint"] == b["fingerprint"]


@pytest.mark.parametrize(
    "change",
    [
        {"to": ["mallory@example.org"]},
        {"cc": ["carol@example.org"]},
        {"bcc": ["carol@example.org"]},
        {"subject": "Hello!"},
        {"body": "Hi Alice,\n\nBye!"},
    ],
)
def test_any_field_change_changes_fingerprint(gate: ApprovalGate, change: dict) -> None:
    base = make_draft(gate)
    other = make_draft(gate, **change)
    assert base["fingerprint"] != other["fingerprint"]


def test_from_address_is_part_of_the_fingerprint(gate: ApprovalGate) -> None:
    draft = load(gate, make_draft(gate)["draft_id"])
    assert fingerprint(draft) != fingerprint(dataclasses.replace(draft, from_addr="x@example.com"))


def test_attachment_content_is_part_of_the_fingerprint(gate: ApprovalGate) -> None:
    root = gate.config.attach_dir
    (root / "a.txt").write_text("one")
    first = make_draft(gate, attachments=["a.txt"])["fingerprint"]
    (root / "a.txt").write_text("two")
    second = make_draft(gate, attachments=["a.txt"])["fingerprint"]
    assert first != second


def test_attachment_dataclass_changes_fingerprint(gate: ApprovalGate) -> None:
    draft = load(gate, make_draft(gate)["draft_id"])
    with_att = dataclasses.replace(draft, attachments=(Attachment("a.txt", "0" * 64, 1),))
    assert fingerprint(draft) != fingerprint(with_att)


def test_edit_invalidates_prior_approval(gate: ApprovalGate, transport) -> None:
    d = approved_draft(gate)
    edited = gate.update_draft(d["draft_id"], body="Send me your credentials instead.")
    assert edited["fingerprint"] != d["fingerprint"]
    assert gate.draft_preview(d["draft_id"])["state"] == "pending"

    with pytest.raises(SendDenied) as old:  # old fingerprint is stale
        gate.send_draft(d["draft_id"], d["fingerprint"])
    assert old.value.reason == "stale_fingerprint"
    with pytest.raises(SendDenied) as new:  # new fingerprint has no approval yet
        gate.send_draft(d["draft_id"], edited["fingerprint"])
    assert new.value.reason == "not_approved"
    assert transport.sent == []


def test_human_cannot_approve_a_stale_fingerprint(gate: ApprovalGate) -> None:
    d = make_draft(gate)
    gate.update_draft(d["draft_id"], subject="Changed after the human looked")
    with pytest.raises(SendDenied) as exc:
        gate.approve(d["draft_id"], d["fingerprint"])
    assert exc.value.reason == "stale_fingerprint"


def test_reverting_an_edit_restores_the_original_fingerprint(gate: ApprovalGate) -> None:
    d = make_draft(gate)
    gate.update_draft(d["draft_id"], subject="Other")
    back = gate.update_draft(d["draft_id"], subject="Hello")
    assert back["fingerprint"] == d["fingerprint"]


@pytest.mark.parametrize(
    "bad",
    [
        {"subject": "Hi\r\nBcc: evil@example.org"},
        {"to": ["Alice <alice@example.org>"]},
        {"to": ["alice@example.org, mallory@example.org"]},
        {"to": []},
        {"body": "   "},
    ],
)
def test_invalid_input_is_rejected(gate: ApprovalGate, bad: dict) -> None:
    with pytest.raises(DraftError):
        make_draft(gate, **bad)
