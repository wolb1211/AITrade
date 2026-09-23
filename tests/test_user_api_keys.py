"""Keys for the public AI interface: minted once, stored hashed, revoked apart.

They are deliberately separate from the strategy keys: a leaked strategy key must
not reach the public API, and resetting one must not touch the other.
"""

from __future__ import annotations

from pathlib import Path

from app.security import API_KEY_PREFIX, api_key_prefix
from app.store import DEFAULT_API_KEY_RPM, SqliteStore


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "api-keys.db")
    store.initialize()
    return store


def test_a_key_is_minted_once_and_stored_hashed(tmp_path: Path) -> None:
    store = _store(tmp_path)

    created = store.create_user_api_key(7, name="我的应用")

    assert created["key"].startswith(API_KEY_PREFIX)
    assert len(created["key"]) == len(API_KEY_PREFIX) + 32
    assert created["key_prefix"] == api_key_prefix(created["key"])
    # The secret itself is nowhere in the record.
    listed = store.list_user_api_keys(7)
    assert len(listed) == 1
    assert "key" not in listed[0]
    assert listed[0]["key_prefix"] == created["key_prefix"]


def test_the_presented_key_resolves_to_its_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create_user_api_key(7)

    found = store.find_user_api_key(created["key"])

    assert found is not None
    assert found["user_id"] == "7"
    assert found["status"] == "active"
    assert found["rpm_limit"] == DEFAULT_API_KEY_RPM
    # Whitespace around a pasted key is forgiven; anything else is not a key.
    assert store.find_user_api_key(f"  {created['key']}  ") is not None
    assert store.find_user_api_key("ak_deadbeef") is None
    assert store.find_user_api_key("") is None


def test_a_revoked_key_stays_revoked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create_user_api_key(7)

    assert store.revoke_user_api_key(7, created["id"]) is True
    assert store.revoke_user_api_key(7, created["id"]) is False

    found = store.find_user_api_key(created["key"])
    assert found is not None and found["status"] == "revoked"


def test_a_key_belongs_to_one_user_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create_user_api_key(7)

    # Another user cannot revoke it, and does not see it.
    assert store.revoke_user_api_key(8, created["id"]) is False
    assert store.list_user_api_keys(8) == []


def test_using_a_key_is_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create_user_api_key(7)

    store.touch_user_api_key(created["id"])
    store.touch_user_api_key(created["id"])

    listed = store.list_user_api_keys(7)[0]
    assert listed["request_count"] == 2
    assert listed["last_used_at"]
