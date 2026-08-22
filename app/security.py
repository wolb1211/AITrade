from __future__ import annotations

import hashlib
import hmac
import secrets


def hash_deployment_key(raw_key: str) -> str:
    normalized = raw_key.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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
