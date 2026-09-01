"""Access-allowlist enforcement in the auth path.

A valid Supabase token is not enough — the user's email must also be on the
admin-managed allowlist (``allowed_emails``). These tests stub the token
validation and the allowlist lookup so they're fast and network-free.
"""
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app import auth, db


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Force "Supabase configured" (it's a read-only property) and a valid token
    # so every test exercises only the allowlist decision. Caches are cleared so
    # results never leak between tests.
    monkeypatch.setattr(
        type(auth.settings), "supabase_configured", property(lambda self: True)
    )
    monkeypatch.setattr(
        db, "get_user_from_token", lambda _t: {"id": "u1", "email": "Trial@Acme.com"}
    )
    auth._CACHE.clear()
    auth._ALLOW_CACHE.clear()
    yield
    auth._CACHE.clear()
    auth._ALLOW_CACHE.clear()


def _creds():
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok")


def test_allowed_email_passes(monkeypatch):
    monkeypatch.setattr(db, "is_email_allowed", lambda _e: True)
    user = auth.current_user(_creds())
    assert user.id == "u1"
    assert user.email == "Trial@Acme.com"


def test_disallowed_email_is_forbidden(monkeypatch):
    monkeypatch.setattr(db, "is_email_allowed", lambda _e: False)
    with pytest.raises(HTTPException) as ei:
        auth.current_user(_creds())
    assert ei.value.status_code == 403


def test_denials_not_cached_so_approval_takes_effect(monkeypatch):
    """A denied user must get in right after being approved (no cached denial)."""
    calls = {"n": 0}

    def _allow(_e):
        calls["n"] += 1
        return calls["n"] > 1  # denied first, approved on the next request

    monkeypatch.setattr(db, "is_email_allowed", _allow)

    with pytest.raises(HTTPException):
        auth.current_user(_creds())
    # Second request re-checks (denial was not cached) and now succeeds.
    user = auth.current_user(_creds())
    assert user.id == "u1"


def test_allow_is_cached(monkeypatch):
    """An allowed decision is cached, so the DB isn't hit on every request."""
    calls = {"n": 0}

    def _allow(_e):
        calls["n"] += 1
        return True

    monkeypatch.setattr(db, "is_email_allowed", _allow)
    auth.current_user(_creds())
    auth.current_user(_creds())
    assert calls["n"] == 1
