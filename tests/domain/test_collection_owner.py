"""Tests for domain.collection_owner.is_own_collection."""

from __future__ import annotations

import pytest

from domain.collection_owner import is_own_collection


class TestFranchiseAlwaysOwn:
    """Franchise/virtual collections have no owner and always survive an "Own" filter."""

    def test_franchise_is_own_even_with_foreign_id(self):
        # A franchise collection carries no user_id; even a mismatched value is ignored.
        assert is_own_collection(999, own_user_id=1, kind="franchise") is True

    def test_franchise_is_own_when_identity_unknown(self):
        assert is_own_collection(None, own_user_id=None, kind="franchise") is True

    def test_franchise_is_own_with_no_owner_field(self):
        assert is_own_collection(None, own_user_id=1, kind="franchise") is True


class TestUnknownIdentityDegradesToAll:
    """Unknown own identity → every collection is own (the non-breaking fallback)."""

    @pytest.mark.parametrize("kind", ["user", "smart"])
    def test_own_when_identity_unknown(self, kind):
        assert is_own_collection(42, own_user_id=None, kind=kind) is True

    def test_own_when_identity_unknown_and_owner_missing(self):
        assert is_own_collection(None, own_user_id=None, kind="user") is True


class TestKnownIdentityOwnership:
    """With a known own id, user/smart collections split on the owner id."""

    @pytest.mark.parametrize("kind", ["user", "smart"])
    def test_own_when_ids_match(self, kind):
        assert is_own_collection(7, own_user_id=7, kind=kind) is True

    @pytest.mark.parametrize("kind", ["user", "smart"])
    def test_foreign_when_ids_differ(self, kind):
        assert is_own_collection(8, own_user_id=7, kind=kind) is False

    def test_foreign_when_owner_id_missing_but_identity_known(self):
        # A user/smart collection with no user_id and a known own id can't be
        # confirmed as ours — treated as foreign (RomM always sends user_id here).
        assert is_own_collection(None, own_user_id=7, kind="user") is False
