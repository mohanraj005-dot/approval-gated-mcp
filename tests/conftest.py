from __future__ import annotations

from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path

import pytest

from approval_gated_mcp import ApprovalGate, Config, FileOutbox


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingTransport:
    """Records sends in memory; can be told to fail to simulate an unknown outcome."""

    def __init__(self) -> None:
        self.sent: list[tuple[EmailMessage, list[str]]] = []
        self.fail_with: Exception | None = None

    def send(self, message: EmailMessage, recipients: Sequence[str]) -> str:
        if self.fail_with:
            raise self.fail_with
        self.sent.append((message, list(recipients)))
        return f"mem:{len(self.sent)}"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGM_SENDING", raising=False)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    attach = tmp_path / "attach"
    attach.mkdir()
    return Config(
        state_dir=tmp_path / "state",
        outbox_dir=tmp_path / "outbox",
        from_addr="Agent@Example.com",
        rate_limit=3,
        attach_dir=attach,
    )


@pytest.fixture
def gate(config: Config, transport: RecordingTransport, clock: FakeClock) -> ApprovalGate:
    return ApprovalGate(config, transport, clock=clock)


@pytest.fixture
def file_gate(config: Config, clock: FakeClock) -> ApprovalGate:
    return ApprovalGate(config, FileOutbox(config.outbox_dir), clock=clock)


def make_draft(gate: ApprovalGate, **overrides: object) -> dict:
    args: dict = {"to": ["alice@example.org"], "subject": "Hello", "body": "Hi Alice,\n\nBye."}
    args.update(overrides)
    return gate.draft(**args)


def approved_draft(gate: ApprovalGate, **overrides: object) -> dict:
    d = make_draft(gate, **overrides)
    gate.approve(d["draft_id"], d["fingerprint"])
    return d
