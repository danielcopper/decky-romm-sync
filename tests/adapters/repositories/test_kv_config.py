"""Tests for ``SqliteKvConfigRepository`` over the ``kv_config`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adapters.repositories.unit_of_work import SqliteUnitOfWork


class TestRoundTrip:
    def test_set_then_get_returns_value(self, uow: SqliteUnitOfWork):
        uow.kv_config.set("device_id", "abc-123")
        assert uow.kv_config.get("device_id") == "abc-123"

    def test_json_encoded_value_is_stored_verbatim(self, uow: SqliteUnitOfWork):
        uow.kv_config.set("save_sort_settings", '{"sort_by_content": true}')
        assert uow.kv_config.get("save_sort_settings") == '{"sort_by_content": true}'


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.kv_config.get("nope") is None


class TestUpsert:
    def test_set_existing_key_overwrites(self, uow: SqliteUnitOfWork):
        uow.kv_config.set("k", "first")
        uow.kv_config.set("k", "second")
        assert uow.kv_config.get("k") == "second"


class TestDelete:
    def test_delete_removes_key(self, uow: SqliteUnitOfWork):
        uow.kv_config.set("k", "v")
        uow.kv_config.delete("k")
        assert uow.kv_config.get("k") is None

    def test_delete_absent_is_idempotent(self, uow: SqliteUnitOfWork):
        uow.kv_config.delete("nope")
        assert uow.kv_config.get("nope") is None
