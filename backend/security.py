"""Authentication primitives for the APG Analyzer.

Two independent concerns live here:

  1. Password hashing via Argon2id (argon2-cffi). Argon2 is the current best-
     practice choice — memory-hard, side-channel resistant, and the winner of
     the Password Hashing Competition. We pin conservative-but-fast parameters
     appropriate for interactive login on a small-team deployment:
         time_cost=3, memory_cost=64MB, parallelism=4
     Hashes are self-describing (store the whole $argon2id$...$ string), so
     upgrading parameters in the future doesn't require a migration.

  2. JWT issue + verify (PyJWT). We use HS256 with a server-side secret loaded
     from APP_JWT_SECRET. Tokens carry:
         sub  — user.id (string)
         uname — user.username (for audit convenience)
         role — 'admin' | 'analyst' | 'viewer'
         exp  — expiration timestamp (default 8h)
         iat  — issue time
     Verification checks the signature + expiration; callers re-check role at
     the endpoint level.

Design notes:
  * No refresh tokens in Phase 5. For a small team, the cost (another token
    type + rotation logic + revocation store) outweighs the benefit. Users
    reauth when the 8-hour token expires.
  * No JWT revocation list. Token revocation = disabling the user account;
    the auth dependency re-checks user.disabled on every request.
  * Constant-time comparison is built into argon2's verify_password; never
    roll your own with `==`.

Env vars:
  APP_JWT_SECRET       — HS256 signing key. REQUIRED in production. Falls back
                         to a randomly-generated per-process secret in dev so
                         you don't get errors during local testing, but this
                         means every uvicorn restart invalidates tokens.
  APP_JWT_EXPIRES_MIN  — token lifetime in minutes (default 480 = 8h).
  APP_ADMIN_USERNAME   — used by backend.db.init_admin, not this module
  APP_ADMIN_PASSWORD   — used by backend.db.init_admin, not this module
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


# PasswordHasher is thread-safe; one instance per process is fine.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,   # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(plaintext: str) -> str:
    """Return an Argon2id hash string for the given password. Minimum 10 chars."""
    if not plaintext or len(plaintext) < 10:
        raise ValueError("Password must be at least 10 characters")
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, stored_hash: str) -> bool:
    """Constant-time verification. Returns False on mismatch or malformed hash."""
    if not plaintext or not stored_hash:
        return False
    try:
        _hasher.verify(stored_hash, plaintext)
    except (VerifyMismatchError, InvalidHash):
        return False
    except Exception as e:
        log.warning("Unexpected error during password verification: %s", e)
        return False
    return True


def needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash was made with older parameters. Call after a
    successful verify and, if True, re-hash with the new parameters and save."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHash:
        return True


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


_JWT_ALG = "HS256"


def _jwt_secret() -> str:
    """Load the HS256 secret from APP_JWT_SECRET, or synthesize an in-memory one."""
    secret = os.getenv("APP_JWT_SECRET")
    if secret:
        return secret
    # Lazy per-process fallback — regenerates on every process start. That's
    # intentionally inconvenient to nudge production users to set the env var.
    global _fallback_secret
    try:
        return _fallback_secret
    except NameError:
        _fallback_secret = secrets.token_urlsafe(48)
        log.warning(
            "APP_JWT_SECRET not set; using an ephemeral per-process secret. "
            "All tokens will be invalidated on restart. Set APP_JWT_SECRET in production."
        )
        return _fallback_secret


def _jwt_expires_minutes() -> int:
    raw = os.getenv("APP_JWT_EXPIRES_MIN", "480")
    try:
        return max(1, int(raw))
    except ValueError:
        return 480


def issue_token(*, user_id: int, username: str, role: str) -> dict:
    """Mint a JWT for the given user. Returns {access_token, expires_at, token_type}."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=_jwt_expires_minutes())
    payload = {
        "sub": str(user_id),
        "uname": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, _jwt_secret(), algorithm=_JWT_ALG)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at.isoformat(),
    }


def decode_token(token: str) -> Optional[dict]:
    """Validate signature + expiration. Return the payload dict on success, else None."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[_JWT_ALG])
    except jwt.ExpiredSignatureError:
        log.info("JWT rejected: expired")
        return None
    except jwt.InvalidTokenError as e:
        log.info("JWT rejected: %s", e)
        return None
    # Minimal sanity check — payload must have `sub` + `role`
    if "sub" not in payload or "role" not in payload:
        return None
    return payload


# ---------------------------------------------------------------------------
# Role helpers
# ---------------------------------------------------------------------------


VALID_ROLES = ("admin", "analyst", "viewer")


def is_valid_role(role: str) -> bool:
    return role in VALID_ROLES


__all__ = [
    "hash_password", "verify_password", "needs_rehash",
    "issue_token", "decode_token",
    "VALID_ROLES", "is_valid_role",
]
