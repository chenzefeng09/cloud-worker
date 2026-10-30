"""Server-side store for browser login sessions (table ``web_sessions``).

Why it exists
-------------
Keycloak access tokens live five minutes.  The BFF used to carry the access
token inside the signed session cookie and had nowhere to keep the refresh
token, so every web session died five minutes after login.  Now the cookie
carries only an opaque, random session id (plus tenant / user, all signed by
:class:`foundation_sdk.CookieSigner`); the tokens live here, server-side, and
the BFF rotates them shortly before the access token expires
(:mod:`agent_bff.api.web_session`).  The refresh token never reaches the
browser.

Storage choices
---------------
* Primary key = SHA-256 of the session id.  The raw id only ever exists in the
  signed cookie, so a database dump yields no usable cookie values.
* Tokens are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256) through
  :class:`cryptography.fernet.MultiFernet`, which gives key rotation for free:
  the first key encrypts, every listed key decrypts, and each refresh rewrites
  the row under the current primary key.  Keys come from
  ``BFF_SESSION_TOKEN_KEYS``; without it a key is derived (HKDF-SHA256) from
  ``BFF_COOKIE_SECRET`` with a purpose-specific salt/info, so it is never the
  cookie-signing key itself.  (``cryptography`` is installed through the
  ``python-jose[cryptography]`` dependency.)
* Each ciphertext is bound to its row, tenant, user and token kind; a blob
  copied into another row or column fails to decrypt.

Row-level security (migration ``0005_web_sessions``) limits the runtime role to
the caller's own rows, plus a narrow policy that lets the expiry sweep delete
rows that are already past ``expires_at``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agent_bff.domain.models import Base, utcnow

ACCESS_TOKEN = "access"
REFRESH_TOKEN = "refresh"

# Actor marker for the expiry sweep; the 0005 RLS policy keys on it.
SWEEP_ACTOR = "SESSION_SWEEP"

_HKDF_SALT = b"agent-bff/web-session"
_HKDF_INFO = b"token-encryption/v1"


class WebSession(Base):
    """One signed-in browser session.  See the module docstring."""

    __tablename__ = "web_sessions"
    __table_args__ = (
        Index("ix_web_sessions_tenant_user", "tenant_id", "user_id"),
        Index("ix_web_sessions_expires_at", "expires_at"),
    )

    # SHA-256 hex of the opaque session id carried in the cookie.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[str] = mapped_column(String(40), nullable=False)
    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    # When the access token stops being accepted downstream (JWT ``exp``).
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL → the session cannot be refreshed and ends with its access token.
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    clearance: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Hard upper bound, aligned with the cookie's own max age.  Foundation
    # identity's idle / absolute session timeouts usually end it sooner (the
    # next refresh is refused).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def new_session_id() -> str:
    """A fresh, unguessable session id (256 bits) for the cookie."""
    return secrets.token_urlsafe(32)


def hash_session_id(session_id: str) -> str:
    """Row key for a session id — the raw id is never stored."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as the UTC they were."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class TokenDecryptError(Exception):
    """A stored token could not be decrypted or is not bound to this row."""


class TokenCipher:
    """Encrypts session tokens at rest, bound to the row they belong to."""

    def __init__(self, keys: Iterable[str], *, fallback_secret: str) -> None:
        fernets: list[Fernet] = []
        for key in keys:
            key = key.strip()
            if not key:
                continue
            try:
                fernets.append(Fernet(key.encode("ascii")))
            except (ValueError, UnicodeEncodeError) as exc:
                # Fail at startup, like an unsafe cookie secret does.
                raise ValueError(
                    "BFF_SESSION_TOKEN_KEYS must be comma-separated Fernet keys "
                    "(32 url-safe base64-encoded bytes each)"
                ) from exc
        if not fernets:
            if not fallback_secret:
                raise ValueError(
                    "web-session token encryption needs BFF_SESSION_TOKEN_KEYS "
                    "or BFF_COOKIE_SECRET"
                )
            fernets.append(Fernet(_derive_key(fallback_secret)))
        self._fernet = MultiFernet(fernets)

    def encrypt(self, token: str, *, binding: str) -> str:
        plaintext = json.dumps({"b": binding, "t": token}, separators=(",", ":"))
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, blob: str, *, binding: str) -> str:
        try:
            data = json.loads(self._fernet.decrypt(blob.encode("ascii")))
        except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
            raise TokenDecryptError("stored token cannot be decrypted") from exc
        if (
            not isinstance(data, dict)
            or data.get("b") != binding
            or not isinstance(data.get("t"), str)
        ):
            raise TokenDecryptError("stored token is not bound to this session")
        return str(data["t"])


def token_binding(row: WebSession, kind: str) -> str:
    """What a ciphertext is bound to: its row, owner and token kind."""
    return f"{row.id}:{row.tenant_id}:{row.user_id}:{kind}"


def _derive_key(secret: str) -> bytes:
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    ).derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)
