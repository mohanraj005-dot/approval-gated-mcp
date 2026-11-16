"""End-to-end walkthrough without an MCP client: draft, denied send, approval, send, replay.

Run:  python examples/demo.py
Writes to a temporary directory, never to a real mail server.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from approval_gated_mcp import ApprovalGate, Config, FileOutbox, SendDenied


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = Config(state_dir=root / "state", outbox_dir=root / "outbox")
        gate = ApprovalGate(config, FileOutbox(config.outbox_dir))

        # 1. The agent drafts. Nothing is sent.
        d = gate.draft(["alice@example.org"], "Project update", "Hi Alice,\n\nShipped.\n")
        print("drafted      ", d)

        # 2. The agent tries to send without approval: denied.
        try:
            gate.send_draft(d["draft_id"], d["fingerprint"])
        except SendDenied as exc:
            print("denied       ", exc.reason)

        # 3. A human reviews the preview and approves that exact fingerprint
        #    (in real use: `agm approve <draft_id> <fingerprint>` in a terminal).
        print("preview      ", gate.draft_preview(d["draft_id"], full=True)["body_excerpt"].strip())
        gate.approve(d["draft_id"], d["fingerprint"], approver="demo-human")

        # 4. An edit after approval invalidates it.
        edited = gate.update_draft(d["draft_id"], body="Hi Alice,\n\nActually, wire the money.")
        try:
            gate.send_draft(d["draft_id"], edited["fingerprint"])
        except SendDenied as exc:
            print("after edit   ", exc.reason)

        # 5. Put the original text back: the original approval applies again, and it sends once.
        gate.update_draft(d["draft_id"], body="Hi Alice,\n\nShipped.\n")
        print(
            "sent         ", gate.send_draft(d["draft_id"], d["fingerprint"])["ref"].split("/")[-1]
        )

        # 6. Replay is refused.
        try:
            gate.send_draft(d["draft_id"], d["fingerprint"])
        except SendDenied as exc:
            print("replay       ", exc.reason)

        print("\naudit log:")
        for row in gate.audit.read():
            print(" ", row["event"], row.get("reason", ""))


if __name__ == "__main__":
    main()
