from __future__ import annotations

import hashlib
import hmac
import secrets


def hash_deployment_key(raw_key: str) -> str:
    normalized = raw_key.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# Keys for the public AI interface. They carry a different prefix from a strategy
# key on purpose: a leaked strategy key must not be usable against the public API,
# and revoking one must not touch the other.
API_KEY_PREFIX = "ak_"
API_KEY_BYTES = 16


def generate_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_hex(API_KEY_BYTES)}"


def hash_api_key(raw_key: str) -> str:
    return hash_deployment_key(raw_key)


def api_key_prefix(raw_key: str) -> str:
    """The part of a key that is safe to show again after it is created."""
    text = raw_key.strip()
    if len(text) <= 12:
        return text
    return f"{text[:10]}…{text[-4:]}"


PASSWORD_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(raw_salt),
            int(raw_iterations),
        )
        return hmac.compare_digest(digest, bytes.fromhex(raw_digest))
    except (TypeError, ValueError):
        return False


def hash_auth_value(value: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
