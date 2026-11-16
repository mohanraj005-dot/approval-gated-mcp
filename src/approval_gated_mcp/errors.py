"""Exception types. Every denial carries a stable machine-readable `reason`."""

from __future__ import annotations


class GateError(Exception):
    """Base class for all errors raised by the gate."""


class DraftError(GateError):
    """The draft input is invalid or the draft does not exist."""


class SendDenied(GateError):  # noqa: N818 - reads better as a verdict than as an error
    """A send was refused. Nothing was transmitted and the draft was not consumed."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


class SendOutcomeUnknown(GateError):  # noqa: N818
    """The transport failed after the draft was claimed. The message may or may not
    have been delivered. The gate records this and never retries on its own."""
