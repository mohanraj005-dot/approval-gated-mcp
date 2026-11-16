"""Approval-gated messaging: agents draft, humans approve by fingerprint, then it sends."""

from .config import Config
from .errors import DraftError, GateError, SendDenied, SendOutcomeUnknown
from .gate import ApprovalGate
from .models import Draft, fingerprint
from .transport import FileOutbox, SmtpTransport, Transport

__all__ = [
    "ApprovalGate",
    "Config",
    "Draft",
    "DraftError",
    "FileOutbox",
    "GateError",
    "SendDenied",
    "SendOutcomeUnknown",
    "SmtpTransport",
    "Transport",
    "fingerprint",
]
__version__ = "0.1.0"
