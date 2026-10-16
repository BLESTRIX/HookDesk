import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from argon2 import PasswordHasher, Type
from cryptography.fernet import Fernet

from app.core.errors import AppError

password_hasher = PasswordHasher(type=Type.ID)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def encrypt_secret(secret: str, key: str) -> str:
    return Fernet(key.encode()).encrypt(secret.encode()).decode()


def decrypt_secret(ciphertext: str, key: str) -> str:
    return Fernet(key.encode()).decrypt(ciphertext.encode()).decode()


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return (
        "sha256="
        + hmac.new(
            secret.encode(), timestamp.encode("ascii") + b"." + body, hashlib.sha256
        ).hexdigest()
    )


def verify_signature(
    secrets_: list[str],
    timestamp: str,
    signature: str,
    body: bytes,
    *,
    now: datetime,
    tolerance: int,
) -> None:
    if not timestamp.isascii() or not timestamp.isdecimal() or len(timestamp) > 12:
        raise AppError(401, "invalid_timestamp", "A Unix timestamp in seconds is required")
    if abs(now.timestamp() - int(timestamp)) > tolerance:
        raise AppError(401, "stale_timestamp", "Timestamp is outside the allowed tolerance")
    # Compare every eligible secret, including during rotation.
    if not signature.isascii() or len(signature) != 71:
        raise AppError(401, "invalid_signature", "Signature verification failed")
    valid = False
    for secret in secrets_:
        valid |= hmac.compare_digest(sign(secret, timestamp, body), signature)
    if not valid:
        raise AppError(401, "invalid_signature", "Signature verification failed")
