from __future__ import annotations

import hashlib
import re


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")


def require_identity(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    if not _IDENTITY.fullmatch(normalized):
        raise ValueError(f"{field} contains unsupported characters")
    return normalized


def stable_uid(namespace: str, *parts: object) -> str:
    encoded = "\x1f".join(
        [require_identity(namespace, "namespace"), *(str(part) for part in parts)]
    ).encode("utf-8")
    return f"{namespace}_{hashlib.sha256(encoded).hexdigest()}"
