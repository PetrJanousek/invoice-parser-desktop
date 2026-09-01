"""Authentication — validate Supabase user access tokens.

Tokens are validated against Supabase's GoTrue server (see
``db.get_user_from_token``), so this works with any project JWT signing
configuration and needs no JWT secret. Validated tokens are cached briefly to
avoid a round-trip on every request.
"""
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .config import settings

_bearer = HTTPBearer(auto_error=True)

_CACHE: dict[str, tuple["User", float]] = {}
_CACHE_TTL = 60.0  # seconds

# Access allowlist decisions. We cache only *allowed* emails (email -> expiry)
# so the hot path skips a DB round-trip; denials are never cached, so approving
# someone in the SQL editor takes effect on their next request (no waiting for a
# TTL to lapse).
_ALLOW_CACHE: dict[str, float] = {}
_ALLOW_TTL = 60.0  # seconds


@dataclass
class User:
    id: str
    email: str | None


def _verify(token: str) -> "User":
    hit = _CACHE.get(token)
    if hit and hit[1] > time.monotonic():
        return hit[0]

    try:
        data = db.get_user_from_token(token)
    except db.SupabaseNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not validate token: {exc}",
        ) from exc

    if not data or not data.get("id"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
        )

    u = User(id=data["id"], email=data.get("email"))
    _CACHE[token] = (u, time.monotonic() + _CACHE_TTL)
    return u


def _enforce_allowlist(user: "User") -> None:
    """Reject an otherwise-valid user whose email is not on the allowlist.

    Signup is open in Supabase, so a valid token alone does not grant access —
    the email must be approved by the admin (``allowed_emails``). Allowed
    results are cached briefly; denials are not (see ``_ALLOW_CACHE``).
    """
    email = (user.email or "").lower()
    hit = _ALLOW_CACHE.get(email)
    if hit and hit > time.monotonic():
        return

    if db.is_email_allowed(email):
        _ALLOW_CACHE[email] = time.monotonic() + _ALLOW_TTL
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "Your email is not approved for access yet. Contact the "
            "administrator to be added."
        ),
    )


def current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> User:
    if not settings.supabase_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth is not configured (Supabase credentials missing).",
        )
    user = _verify(creds.credentials)
    _enforce_allowlist(user)
    return user
