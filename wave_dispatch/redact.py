"""Process-wide secret redaction, applied where data leaves the process or is persisted:
audit_trail.jsonl lines, EventBus events (dashboard WebSocket, Discord), and Discord embeds.

Exact secrets are registered at runtime (the Discord token, when the bridge loads it);
token-shaped patterns are caught even when nothing was registered.
"""
from __future__ import annotations

import re
import threading

_secrets: set[str] = set()
_lock = threading.Lock()

PATTERNS = [
    re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,40}"),      # Discord bot token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z\d_-]{20,}"),                                    # API keys
    re.compile(r"\bgh[pousr]_[A-Za-z\d]{30,}"),                                          # GitHub
    re.compile(r"\bxox[abprs]-[A-Za-z\d-]{10,}"),                                        # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                                 # AWS key id
]
REDACTED = "[REDACTED]"


def register(secret: str) -> None:
    if secret and len(secret) >= 8:
        with _lock:
            _secrets.add(secret)


def scrub(text: str) -> str:
    """Replace registered secrets and token-shaped strings. Cheap when nothing matches."""
    for s in tuple(_secrets):
        if s in text:
            text = text.replace(s, REDACTED)
    for pat in PATTERNS:
        text = pat.sub(REDACTED, text)
    return text
