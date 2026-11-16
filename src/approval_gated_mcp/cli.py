"""`agm`: the human side. Approval happens here, out-of-band from the agent."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from .config import KILL_FILE, Config
from .errors import GateError
from .gate import ApprovalGate
from .transport import build_transport


def format_preview(p: dict[str, Any]) -> str:
    atts = (
        "\n".join(
            f"    {a['name']}  {a['size']} bytes  sha256 {a['sha256']}" for a in p["attachments"]
        )
        or "    (none)"
    )
    return (
        f"Draft        {p['draft_id']}  [{p['state']}]\n"
        f"From         {p['from']}\n"
        f"To           {', '.join(p['to'])}\n"
        f"Cc           {', '.join(p['cc']) or '(none)'}\n"
        f"Bcc          {', '.join(p['bcc']) or '(none)'}\n"
        f"Subject      {p['subject']}\n"
        f"Attachments\n{atts}\n"
        f"Fingerprint  {p['fingerprint']}\n"
        f"{'-' * 60}\n{p['body_excerpt']}\n{'-' * 60}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agm", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    ap = sub.add_parser("approve", help="approve one exact draft by fingerprint")
    ap.add_argument("draft_id")
    ap.add_argument("fingerprint")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    pv = sub.add_parser("preview", help="show the full draft exactly as it would be sent")
    pv.add_argument("draft_id")
    sub.add_parser("list", help="list drafts and their state")
    au = sub.add_parser("audit", help="show recent audit events")
    au.add_argument("-n", type=int, default=20)
    sw = sub.add_parser("sending", help="kill switch: on | off | status")
    sw.add_argument("mode", choices=["on", "off", "status"])
    args = parser.parse_args(argv)

    config = Config.from_env()
    gate = ApprovalGate(config, build_transport(config))
    try:
        if args.cmd == "preview":
            print(format_preview(gate.draft_preview(args.draft_id, full=True)))
        elif args.cmd == "list":
            for d in gate.store.list_drafts():
                p = gate.draft_preview(d.draft_id)
                print(f"{d.draft_id}  {p['state']:<16} {p['fingerprint']}  {d.subject}")
        elif args.cmd == "audit":
            for row in gate.audit.read(args.n):
                print(json.dumps(row, sort_keys=True))
        elif args.cmd == "sending":
            kill = config.state_dir / KILL_FILE
            if args.mode == "off":
                kill.touch()
            elif args.mode == "on":
                kill.unlink(missing_ok=True)
            print(config.kill_switch_reason() or "sending is enabled")
        else:  # approve
            preview = gate.draft_preview(args.draft_id, full=True)
            print(format_preview(preview))
            if preview["fingerprint"] != args.fingerprint:
                print(
                    f"\nRefused: draft changed. Current fingerprint is {preview['fingerprint']}",
                    file=sys.stderr,
                )
                return 2
            if not args.yes:
                if not sys.stdin.isatty():
                    print("\nRefused: needs an interactive terminal (or --yes).", file=sys.stderr)
                    return 2
                if input("\nApprove exactly this message? [y/N] ").strip().lower() != "y":
                    print("Not approved.")
                    return 1
            gate.approve(args.draft_id, args.fingerprint, approver="cli")
            print(f"Approved {args.draft_id} at fingerprint {args.fingerprint}.")
    except GateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
