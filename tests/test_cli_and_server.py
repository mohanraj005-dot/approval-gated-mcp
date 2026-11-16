from __future__ import annotations

import asyncio

import pytest

from approval_gated_mcp import ApprovalGate, Config
from approval_gated_mcp.cli import main

from .conftest import RecordingTransport, make_draft


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, config: Config) -> Config:
    monkeypatch.setenv("AGM_HOME", str(config.state_dir))
    monkeypatch.setenv("AGM_OUTBOX", str(config.outbox_dir))
    return config


def test_cli_approve_enables_send(cli_env: Config, transport, capsys) -> None:
    gate = ApprovalGate(cli_env, transport)  # real clock, like the CLI process
    d = make_draft(gate)
    assert main(["approve", d["draft_id"], d["fingerprint"], "--yes"]) == 0
    assert "Hi Alice" in capsys.readouterr().out  # the human sees the full body
    assert gate.send_draft(d["draft_id"], d["fingerprint"])["status"] == "sent"
    assert any(e["actor"] == "cli" for e in gate.audit.read() if e["event"] == "approve")


def test_cli_refuses_stale_fingerprint(cli_env: Config, gate: ApprovalGate) -> None:
    d = make_draft(gate)
    gate.update_draft(d["draft_id"], body="different")
    assert main(["approve", d["draft_id"], d["fingerprint"], "--yes"]) == 2
    assert gate.draft_preview(d["draft_id"])["state"] == "pending"


def test_cli_refuses_without_tty_or_yes(cli_env: Config, gate: ApprovalGate, monkeypatch) -> None:
    d = make_draft(gate)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["approve", d["draft_id"], d["fingerprint"]]) == 2


def test_cli_kill_switch_toggle(cli_env: Config, gate: ApprovalGate) -> None:
    assert main(["sending", "off"]) == 0
    assert gate.config.kill_switch_reason() is not None
    assert main(["sending", "on"]) == 0
    assert gate.config.kill_switch_reason() is None


def test_mcp_server_exposes_no_approve_tool(config: Config) -> None:
    pytest.importorskip("mcp")
    from approval_gated_mcp.server import build_server

    server = build_server(ApprovalGate(config, RecordingTransport()))
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"draft", "update_draft", "draft_preview", "send_draft"}
    assert not any("approve" in n for n in names)


def test_end_to_end_over_stdio(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real MCP client <-> real server process: draft, denied send, CLI approval, send."""
    pytest.importorskip("mcp")
    import json
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        **os.environ,
        "AGM_HOME": str(config.state_dir),
        "AGM_OUTBOX": str(config.outbox_dir),
        "AGM_FROM": "agent@example.com",
    }
    monkeypatch.setenv("AGM_HOME", env["AGM_HOME"])
    monkeypatch.setenv("AGM_OUTBOX", env["AGM_OUTBOX"])
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "approval_gated_mcp.server"], env=env
    )

    async def scenario() -> tuple[bool, bool, str]:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            made = await session.call_tool(
                "draft", {"to": ["a@example.org"], "subject": "Hi", "body": "Hello"}
            )
            d = json.loads(made.content[0].text)
            args = {"draft_id": d["draft_id"], "fingerprint": d["fingerprint"]}
            before = await session.call_tool("send_draft", args)
            assert main(["approve", d["draft_id"], d["fingerprint"], "--yes"]) == 0  # human step
            after = await session.call_tool("send_draft", args)
            denied = bool(getattr(before, "is_error", getattr(before, "isError", False)))
            ok = not bool(getattr(after, "is_error", getattr(after, "isError", False)))
            return denied, ok, before.content[0].text

    denied, ok, text = asyncio.run(scenario())
    assert denied and "not_approved" in text
    assert ok
    assert len(list(config.outbox_dir.glob("*.eml"))) == 1
