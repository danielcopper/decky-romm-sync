"""Tests for domain/download_frames.py — the terminal download_progress payload."""

from __future__ import annotations

from domain.download_frames import cancelled_frame


class TestCancelledFrame:
    def test_projects_every_queue_field_onto_the_frame(self):
        entry = {
            "rom_name": "Some Game",
            "platform_name": "Nintendo 64",
            "file_name": "some-game.z64",
            "progress": 42,
            "bytes_downloaded": 4200,
            "total_bytes": 10000,
            "resumable": True,
            "_target_path": "/roms/n64/some-game.z64",
        }
        assert cancelled_frame(7, entry) == {
            "rom_id": 7,
            "rom_name": "Some Game",
            "platform_name": "Nintendo 64",
            "file_name": "some-game.z64",
            "status": "cancelled",
            "progress": 42,
            "bytes_downloaded": 4200,
            "total_bytes": 10000,
            "resumable": True,
        }

    def test_an_empty_entry_yields_the_full_shape_with_defaults(self):
        """The frame's key set is the wire contract — never narrowed by a sparse entry."""
        assert cancelled_frame(7, {}) == {
            "rom_id": 7,
            "rom_name": "",
            "platform_name": "",
            "file_name": "",
            "status": "cancelled",
            "progress": 0,
            "bytes_downloaded": 0,
            "total_bytes": 0,
            "resumable": False,
        }

    def test_private_queue_keys_never_ride_the_wire(self):
        frame = cancelled_frame(7, {"_target_path": "/roms/n64/g.z64", "_control": object()})
        assert "_target_path" not in frame
        assert "_control" not in frame
