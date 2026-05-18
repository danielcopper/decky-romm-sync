"""Tests for RomInfoService — per-ROM save path resolution and local save discovery."""

from tests.services.saves._helpers import (
    _create_save,
    _install_rom,
    make_service,
)


class TestFindSaveFiles:
    """Tests for find_save_files."""

    def test_finds_srm(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, system="gba", rom_name="pokemon")

        result = svc._rom_info.find_save_files(42)

        assert len(result) == 1
        assert result[0]["filename"] == "pokemon.srm"
        assert result[0]["path"].endswith("pokemon.srm")

    def test_finds_rtc_companion(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, file_name="emerald.gba")
        _create_save(tmp_path, system="gba", rom_name="emerald", ext=".srm")
        _create_save(tmp_path, system="gba", rom_name="emerald", ext=".rtc", content=b"\x02" * 16)

        result = svc._rom_info.find_save_files(42)

        filenames = sorted(f["filename"] for f in result)
        assert filenames == ["emerald.rtc", "emerald.srm"]

    def test_multi_disc_uses_m3u_name(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._state["installed_roms"]["55"] = {
            "rom_id": 55,
            "file_name": "FF7.zip",
            "file_path": str(tmp_path / "retrodeck" / "roms" / "psx" / "FF7" / "Final Fantasy VII.m3u"),
            "system": "psx",
            "platform_slug": "psx",
            "rom_dir": str(tmp_path / "retrodeck" / "roms" / "psx" / "FF7"),
            "installed_at": "2026-01-01T00:00:00",
        }
        # With sort_by_content=True, saves land in saves_base/{content_dir} where
        # content_dir = last folder component of the ROM's directory = "FF7"
        saves_dir = tmp_path / "saves" / "FF7"
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / "Final Fantasy VII.srm").write_bytes(b"\x00" * 1024)

        result = svc._rom_info.find_save_files(55)

        assert any(f["filename"] == "Final Fantasy VII.srm" for f in result)

    def test_no_save_file_returns_empty(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=10, system="n64", file_name="zelda.z64")
        (tmp_path / "saves" / "n64").mkdir(parents=True, exist_ok=True)

        result = svc._rom_info.find_save_files(10)

        assert result == []

    def test_saves_dir_not_exists_returns_empty(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = svc._rom_info.find_save_files(42)

        assert result == []

    def test_rom_not_installed_returns_empty(self, tmp_path):
        svc, _ = make_service(tmp_path)

        result = svc._rom_info.find_save_files(999)

        assert result == []


class TestGetRomSaveInfo:
    """Tests for get_rom_save_info."""

    def test_returns_info_for_installed_rom(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = svc._rom_info.get_rom_save_info(42)

        assert result is not None
        assert result["system"] == "gba"
        assert result["rom_name"] == "pokemon"
        assert result["saves_dir"].endswith("saves/gba")

    def test_returns_none_for_missing_rom(self, tmp_path):
        svc, _ = make_service(tmp_path)

        result = svc._rom_info.get_rom_save_info(999)

        assert result is None

    def test_returns_none_for_empty_system(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_name": "game.gba",
            "file_path": "/some/path.gba",
            "system": "",
            "platform_slug": "",
            "installed_at": "2026-01-01T00:00:00",
        }

        result = svc._rom_info.get_rom_save_info(42)

        assert result is None

    # ------------------------------------------------------------------
    # Regression tests for issue #238 — Rule 1: when a save-sort migration
    # is pending, prefer save_sort_settings_previous so sync reads the
    # layout RetroArch actually wrote to during the session that just
    # ended.
    # ------------------------------------------------------------------

    def test_get_rom_save_info_prefers_previous_sort_settings_when_migration_pending(self, tmp_path):
        """Pending migration: previous (OLD) sort settings override current (NEW) (#238)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        # NEW layout (what settings currently say):
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}
        # OLD layout (what the session actually wrote to):
        svc._state["save_sort_settings_previous"] = {"sort_by_content": True, "sort_by_core": False}

        result = svc._rom_info.get_rom_save_info(42)

        assert result is not None
        # OLD layout: no /mGBA subdir.
        assert result["saves_dir"].endswith("saves/gba")
        assert "/mGBA" not in result["saves_dir"]

    def test_get_rom_save_info_uses_current_sort_settings_when_no_pending_migration(self, tmp_path):
        """No pending migration: use current sort settings (#238)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        # Only save_sort_settings is present — no pending migration key at all.
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}
        assert "save_sort_settings_previous" not in svc._state

        result = svc._rom_info.get_rom_save_info(42)

        assert result is not None
        # CURRENT layout: /mGBA subdir is appended because sort_by_core=True.
        assert result["saves_dir"].endswith("saves/gba/mGBA")

    def test_pending_sort_settings_rejects_empty_dict_half_state(self, tmp_path):
        """Empty-dict ``save_sort_settings_previous`` must NOT count as pending (#238 review).

        Freezes the contract: ``get_rom_save_info`` and ``is_save_sort_changed``
        must agree on what counts as pending. Before ``pending_sort_settings``
        was introduced, a literal empty dict at ``save_sort_settings_previous``
        would put the service in a half-state — ``get_rom_save_info`` would
        fall back to current settings (``{} or current``), but
        ``is_save_sort_changed`` would treat the same ``{}`` as pending
        (``is not None``). This test locks in the agreement.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        # Half-state input: empty previous, populated current (NEW).
        svc._state["save_sort_settings_previous"] = {}
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        # Both call sites must agree there is NO pending migration.
        assert svc._rom_info.is_save_sort_changed() is False
        assert svc._rom_info.pending_sort_settings() is None

        result = svc._rom_info.get_rom_save_info(42)
        assert result is not None
        # Reads CURRENT settings (NEW layout), not the empty previous —
        # mGBA subdir is appended because sort_by_core=True.
        assert result["saves_dir"].endswith("saves/gba/mGBA")

    # ------------------------------------------------------------------
    # Regression tests for issue #232 — RomInfoService must resolve the
    # RetroArch ``corename`` via the .info parser when sort_by_core is
    # active, and must fall back with a warning when it cannot.
    # ------------------------------------------------------------------

    def test_default_sort_only_by_content_no_core_subdir(self, tmp_path):
        """sort_by_core=False (RetroDECK default) → no core subdir."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": False}

        result = svc._rom_info.get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        assert "/mGBA" not in result["saves_dir"]

    def test_sort_by_core_appends_retroarch_corename(self, tmp_path):
        """sort_by_core=True with resolvable corename → saves_dir ends in /{system}/{corename}."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        result = svc._rom_info.get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba/mGBA")

    def test_sort_by_core_uses_corename_not_es_de_label(self, tmp_path):
        """The RetroArch .info corename (``Snes9x``) must be used, not the ES-DE label (``Snes9x - Current``)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x - Current"),
            get_core_name=lambda core_so: "Snes9x",
        )
        _install_rom(svc, tmp_path, system="snes", file_name="mario.sfc")
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        result = svc._rom_info.get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/snes/Snes9x")
        assert "Snes9x - Current" not in result["saves_dir"]

    def test_sort_by_core_falls_back_when_corename_none(self, tmp_path, caplog):
        """sort_by_core=True but corename unresolvable → warn + fall back to parent dir.

        The warning must include ``core_so=mgba_libretro`` so a user can identify
        which ``.info`` file the parser failed on.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: None,  # .info unreadable / field missing
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        with caplog.at_level("WARNING"):
            result = svc._rom_info.get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        assert "/mGBA" not in result["saves_dir"]
        warnings = [rec.message for rec in caplog.records if "unable to resolve RetroArch corename" in rec.message]
        assert warnings, "expected fallback warning"
        assert "core_so=mgba_libretro" in warnings[0]

    def test_sort_by_core_falls_back_when_get_core_name_returns_none(self, tmp_path, caplog):
        """``get_core_name`` returns ``None`` (.info unreadable) → warns and falls back.

        ``get_active_core`` succeeded so ``core_so`` is identified in the
        diagnostic log to help the user locate the unreadable .info file.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: None,
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        with caplog.at_level("WARNING"):
            result = svc._rom_info.get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        warnings = [rec.message for rec in caplog.records if "unable to resolve RetroArch corename" in rec.message]
        assert warnings, "expected fallback warning"
        assert "core_so=mgba_libretro" in warnings[0]

    def test_sort_by_core_falls_back_when_active_core_unresolved(self, tmp_path, caplog):
        """sort_by_core=True but get_active_core returns (None, None) → warn + fall back.

        When ES-DE cannot determine the active core, ``core_so`` is ``None`` and
        the log records ``core_so=unresolved``.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: (None, None),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        with caplog.at_level("WARNING"):
            result = svc._rom_info.get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        warnings = [rec.message for rec in caplog.records if "unable to resolve RetroArch corename" in rec.message]
        assert warnings, "expected fallback warning"
        assert "core_so=unresolved" in warnings[0]

    def test_resolve_retroarch_corename_happy_path(self, tmp_path):
        """Direct test of the helper: both callbacks resolve → (corename, core_so) tuple returned."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x - Current"),
            get_core_name=lambda core_so: "Snes9x",
        )
        assert svc._rom_info.resolve_retroarch_corename("snes", "mario.sfc") == ("Snes9x", "snes9x_libretro")

    def test_resolve_retroarch_corename_returns_none_tuple_when_core_so_empty(self, tmp_path):
        """ES-DE returns (None, None) → helper returns (None, None)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: (None, None),
            get_core_name=lambda core_so: "Snes9x",
        )
        assert svc._rom_info.resolve_retroarch_corename("snes", "mario.sfc") == (None, None)

    def test_resolve_retroarch_corename_preserves_core_so_when_corename_empty(self, tmp_path):
        """Empty corename with resolved core_so → (None, core_so).

        The core_so is preserved in the second element so the caller can log
        which ``.info`` file failed diagnostically. The first element is None
        because the empty-string corename is treated as "no usable value".
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x"),
            get_core_name=lambda core_so: "",
        )
        assert svc._rom_info.resolve_retroarch_corename("snes", "mario.sfc") == (None, "snes9x_libretro")
