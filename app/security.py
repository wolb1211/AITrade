from __future__ import annotations

import hashlib


def hash_deployment_key(raw_key: str) -> str:
    normalized = raw_key.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

