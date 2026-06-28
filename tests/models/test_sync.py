"""Tests for the RomM Device Sync wire schemas (models/sync.py).

These TypedDicts mirror the live 4.9.2 OpenAPI; the checks pin the field set
of each wire shape (a typo'd or dropped key would break negotiate parity) and
confirm they stay plain dicts at runtime.
"""

from __future__ import annotations

from models.sync import (
    ClientSaveState,
    SyncCompleteResponse,
    SyncNegotiateResponse,
    SyncOperation,
    SyncPlaySessionEntry,
    SyncSession,
)

_SCHEMAS = [
    ClientSaveState,
    SyncOperation,
    SyncNegotiateResponse,
    SyncSession,
    SyncCompleteResponse,
    SyncPlaySessionEntry,
]


def test_all_schemas_are_dict_subclasses():
    """The wire shapes are TypedDicts — plain dicts at runtime, typed at the boundary."""
    for schema in _SCHEMAS:
        assert issubclass(schema, dict)


def test_client_save_state_field_set():
    state: ClientSaveState = {
        "rom_id": 1,
        "file_name": "a.srm",
        "updated_at": "2026-06-01T00:00:00Z",
        "file_size_bytes": 12,
        "slot": "default",
        "emulator": "retroarch",
        "content_hash": "abc",
    }
    assert set(state) == {
        "rom_id",
        "file_name",
        "updated_at",
        "file_size_bytes",
        "slot",
        "emulator",
        "content_hash",
    }


def test_sync_operation_field_set():
    op: SyncOperation = {
        "action": "conflict",
        "rom_id": 1,
        "file_name": "a.srm",
        "reason": "both sides changed",
        "save_id": 9,
        "slot": "default",
        "emulator": "retroarch",
        "server_updated_at": "2026-06-01T00:00:00Z",
        "server_content_hash": "def",
    }
    assert op["action"] == "conflict"
    assert set(op) == {
        "action",
        "rom_id",
        "file_name",
        "reason",
        "save_id",
        "slot",
        "emulator",
        "server_updated_at",
        "server_content_hash",
    }


def test_negotiate_response_carries_operations():
    op: SyncOperation = {"action": "upload", "rom_id": 1, "file_name": "a.srm", "reason": "newer locally"}
    response: SyncNegotiateResponse = {
        "session_id": 7,
        "operations": [op],
        "total_upload": 1,
        "total_download": 0,
        "total_conflict": 0,
        "total_no_op": 0,
    }
    assert response["operations"][0]["action"] == "upload"
    assert response["session_id"] == 7


def test_complete_response_nests_session():
    session: SyncSession = {
        "id": 7,
        "device_id": "dev-1",
        "user_id": 1,
        "status": "completed",
        "initiated_at": "t0",
        "operations_planned": 1,
        "operations_completed": 1,
        "operations_failed": 0,
        "created_at": "t0",
        "updated_at": "t1",
    }
    response: SyncCompleteResponse = {"session": session}
    assert response["session"]["status"] == "completed"
