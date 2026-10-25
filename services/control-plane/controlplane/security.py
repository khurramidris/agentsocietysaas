"""Passwords, tokens, at-rest encryption and signed URLs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .settings import Settings

_hasher = PasswordHasher()

API_KEY_PREFIX = "sk"
PROJECT_TOKEN_PREFIX = "sbx"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# ----------------------------------------------------------------- access JWTs


def create_access_token(settings: Settings, user_id: str) -> str:
    now = int(time.time())
    payload = {"sub": user_id, "typ": "access", "iat": now, "exp": now + settings.access_token_ttl_s, "jti": secrets.token_hex(8)}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_access_token(settings: Settings, token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "access":
        return None
    return str(payload["sub"])


# ----------------------------------------------------------- composite secrets


@dataclass(frozen=True)
class CompositeToken:
    """``<kind>_<id>_<secret>``: the id locates the row, only the secret's hash is stored."""

    kind: str
    ident: str
    secret: str

    @property
    def value(self) -> str:
        return f"{self.kind}_{self.ident}_{self.secret}"


def new_composite(kind: str, ident: str | None = None) -> CompositeToken:
    return CompositeToken(kind=kind, ident=ident or secrets.token_hex(6), secret=secrets.token_hex(24))


def parse_composite(value: str) -> CompositeToken | None:
    parts = value.strip().split("_")
    if len(parts) != 3 or not all(parts):
        return None
    kind, ident, secret = parts
    if kind not in (API_KEY_PREFIX, PROJECT_TOKEN_PREFIX):
        return None
    if not all(c in "0123456789abcdef" for c in ident + secret):
        return None
    return CompositeToken(kind, ident, secret)


def secret_matches(stored_hash: str, secret: str) -> bool:
    return hmac.compare_digest(stored_hash, sha256_hex(secret))


# ------------------------------------------------------------------- encryption


class Encryptor:
    """Fernet encryption for provider API keys and sandbox secrets at rest."""

    def __init__(self, settings: Settings) -> None:
        key = settings.encryption_key.strip()
        if not key:
            # Development fallback, rejected by validate_for_production().
            key = base64.urlsafe_b64encode(hashlib.sha256(("enc:" + settings.secret_key).encode()).digest()).decode()
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str | None) -> str:
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return ""


# ------------------------------------------------------------- signed payloads


def sign(settings: Settings, purpose: str, payload: str, expires_at: int) -> str:
    msg = f"{purpose}|{payload}|{expires_at}".encode()
    return hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()


def verify_signature(settings: Settings, purpose: str, payload: str, expires_at: int, signature: str) -> bool:
    if expires_at < int(time.time()):
        return False
    return hmac.compare_digest(sign(settings, purpose, payload, expires_at), signature)


def make_ticket(settings: Settings, purpose: str, payload: str, ttl_s: int) -> str:
    """Self-contained signed ticket ``payload.exp.sig`` (payload must not contain '.')."""
    exp = int(time.time()) + ttl_s
    return f"{payload}.{exp}.{sign(settings, purpose, payload, exp)}"


def read_ticket(settings: Settings, purpose: str, ticket: str) -> str | None:
    try:
        payload, exp_s, sig = ticket.rsplit(".", 2)
        exp = int(exp_s)
    except ValueError:
        return None
    return payload if verify_signature(settings, purpose, payload, exp, sig) else None
