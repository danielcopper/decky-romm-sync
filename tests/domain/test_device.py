"""Unit tests for the ``Device`` aggregate."""

from __future__ import annotations

import pytest

from domain.device import Device


class TestRegister:
    def test_register_with_name_sets_both_fields(self):
        device = Device.register("dev-123", "Steam Deck")
        assert device.device_id == "dev-123"
        assert device.device_name == "Steam Deck"

    def test_register_without_name_defaults_to_none(self):
        device = Device.register("dev-123")
        assert device.device_id == "dev-123"
        assert device.device_name is None

    def test_register_with_empty_id_raises(self):
        with pytest.raises(ValueError, match="device_id is required"):
            Device.register("")


class TestRename:
    def test_rename_changes_display_name(self):
        device = Device.register("dev-123", "Old Name")
        device.rename("New Name")
        assert device.device_name == "New Name"

    def test_rename_from_none(self):
        device = Device.register("dev-123")
        device.rename("First Name")
        assert device.device_name == "First Name"
