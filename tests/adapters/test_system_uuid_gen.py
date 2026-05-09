"""Tests for the SystemUuidGen adapter — wraps uuid.uuid4."""

from __future__ import annotations

import uuid

from adapters.system_uuid_gen import SystemUuidGen


class TestSystemUuidGen:
    def test_uuid4_returns_uuid_instance_version_4(self):
        gen = SystemUuidGen()
        result = gen.uuid4()
        assert isinstance(result, uuid.UUID)
        assert result.version == 4

    def test_consecutive_calls_return_distinct_uuids(self):
        gen = SystemUuidGen()
        first = gen.uuid4()
        second = gen.uuid4()
        assert first != second
