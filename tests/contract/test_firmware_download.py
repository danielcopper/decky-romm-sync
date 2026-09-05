"""Contract tests for the per-row BIOS download over the real nesting (#164).

``download_platform_firmware_file`` is the Library page's per-file Download
button. It addresses the file by NAME within the platform, so what this tier
pins is the scoping the name is resolved under — the same firmware-slug mapping
``download_all_firmware`` uses — plus the two answers a press can get that the
service tier states in isolation: a name the platform's listing does not hold,
and a file already sitting at its destination.

The bytes land through the real ``FirmwareFileAdapter`` under ``tmp_path``, and
the download record lands through the real Unit of Work, so a passing happy path
also proves the placement and the record the later Delete BIOS is authorized by.
"""

from __future__ import annotations

import os

_DC_FIRMWARE = [
    {
        "id": 1,
        "file_name": "dc_boot.bin",
        "file_path": "bios/dc/dc_boot.bin",
        "file_size_bytes": 8,
        "md5_hash": "",
    },
    {
        "id": 2,
        "file_name": "dc_flash.bin",
        "file_path": "bios/dc/dc_flash.bin",
        "file_size_bytes": 8,
        "md5_hash": "",
    },
]


async def test_downloads_the_named_file_and_records_it(harness):
    harness.romm.firmware_files = [dict(f) for f in _DC_FIRMWARE]
    harness.romm.download_payloads["firmware:1:dc_boot.bin"] = b"bootbios"

    result = await harness.plugin.download_platform_firmware_file("dc", "dc_boot.bin")

    assert result["success"] is True
    assert result["downloaded"] == 1
    with harness.uow_factory() as uow:
        record = uow.bios_files.get("dc", "dc_boot.bin")
    assert record is not None
    assert record.firmware_id == 1
    assert os.path.exists(record.file_path)
    # The sibling row is untouched — one press fetches one file.
    with harness.uow_factory() as uow:
        assert uow.bios_files.get("dc", "dc_flash.bin") is None


async def test_a_second_press_finds_the_file_already_here(harness):
    harness.romm.firmware_files = [dict(f) for f in _DC_FIRMWARE]
    harness.romm.download_payloads["firmware:1:dc_boot.bin"] = b"bootbios"

    assert (await harness.plugin.download_platform_firmware_file("dc", "dc_boot.bin"))["downloaded"] == 1
    result = await harness.plugin.download_platform_firmware_file("dc", "dc_boot.bin")

    assert result["success"] is True
    assert result["downloaded"] == 0
    assert "already here" in result["message"]


async def test_a_name_outside_the_platform_is_refused(harness):
    harness.romm.firmware_files = [dict(f) for f in _DC_FIRMWARE]

    result = await harness.plugin.download_platform_firmware_file("n64", "dc_boot.bin")

    assert result["success"] is False
    assert result["reason"] == "not_in_library"
    assert isinstance(result["message"], str)
    assert result["message"]
    assert result["downloaded"] == 0
    assert "download_firmware" not in [name for name, _args, _kwargs in harness.romm.call_log]


async def test_an_unreachable_server_reports_its_own_reason(harness):
    harness.romm.firmware_files = [dict(f) for f in _DC_FIRMWARE]
    harness.romm.download_firmware_side_effect = OSError("Connection reset")

    result = await harness.plugin.download_platform_firmware_file("dc", "dc_boot.bin")

    assert result["success"] is False
    assert result["reason"]
    assert result["downloaded"] == 0
    with harness.uow_factory() as uow:
        assert uow.bios_files.get("dc", "dc_boot.bin") is None
