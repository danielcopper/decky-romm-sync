"""Tests for the native play-session ingest wire schemas (models/play_sessions.py).

These TypedDicts mirror RomM's standalone ``/api/play-sessions`` contract; the
checks pin each shape's field set (a typo'd or dropped key would break ingest
parity) and confirm they stay plain dicts at runtime.
"""

from __future__ import annotations

from models.play_sessions import (
    PlaySessionIngestEntry,
    PlaySessionIngestResponse,
    PlaySessionIngestResult,
)

_SCHEMAS = [
    PlaySessionIngestEntry,
    PlaySessionIngestResult,
    PlaySessionIngestResponse,
]


def test_all_schemas_are_dict_subclasses():
    """The wire shapes are TypedDicts — plain dicts at runtime, typed at the boundary."""
    for schema in _SCHEMAS:
        assert issubclass(schema, dict)


def test_ingest_entry_field_set():
    entry: PlaySessionIngestEntry = {
        "rom_id": 1,
        "start_time": "2026-06-01T00:00:00Z",
        "end_time": "2026-06-01T01:00:00Z",
        "duration_ms": 3_600_000,
    }
    assert set(entry) == {"rom_id", "start_time", "end_time", "duration_ms"}


def test_ingest_result_created_carries_id():
    result: PlaySessionIngestResult = {"index": 0, "status": "created", "id": 42}
    assert result["status"] == "created"
    assert result["id"] == 42
    assert set(result) == {"index", "status", "id"}


def test_ingest_result_duplicate_id_optional():
    """A duplicate carries no new id — ``id`` is NotRequired, so it may be omitted."""
    result: PlaySessionIngestResult = {"index": 3, "status": "duplicate"}
    assert result["status"] == "duplicate"
    assert set(result) == {"index", "status"}


def test_ingest_response_nests_results_and_counts():
    response: PlaySessionIngestResponse = {
        "results": [{"index": 0, "status": "created", "id": 7}],
        "created_count": 1,
        "skipped_count": 0,
    }
    assert response["results"][0]["status"] == "created"
    assert response["created_count"] == 1
    assert response["skipped_count"] == 0
