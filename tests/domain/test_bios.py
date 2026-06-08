"""Tests for domain.bios pure functions."""

from dataclasses import asdict

from domain.bios import (
    AvailableCore,
    BiosFileEntry,
    BiosStatus,
    build_cores_info,
    build_file_entry,
    classify_firmware_file,
    collect_firmware_status,
    compute_bios_label,
    compute_bios_level,
    format_bios_status,
    is_used_by_active_core,
)


class TestFormatBiosStatusFullDict:
    """Test with a fully-populated bios dict."""

    def test_all_fields_mapped_correctly(self):
        """All fields from a full bios dict are mapped to the frontend-ready dict."""
        bios = {
            "server_count": 3,
            "local_count": 2,
            "all_downloaded": False,
            "required_count": 2,
            "required_downloaded": 1,
            "files": [{"file_name": "gba_bios.bin", "downloaded": True}],
            "active_core": "mgba_libretro",
            "active_core_label": "mGBA",
            "available_cores": [{"core": "mgba_libretro", "label": "mGBA"}],
        }
        result = format_bios_status(bios, "gba")

        assert isinstance(result, BiosStatus)
        assert result.platform_slug == "gba"
        assert result.server_count == 3
        assert result.local_count == 2
        assert result.all_downloaded is False
        assert result.required_count == 2
        assert result.required_downloaded == 1
        assert len(result.files) == 1
        assert result.files[0].file_name == "gba_bios.bin"
        assert result.active_core == "mgba_libretro"
        assert result.active_core_label == "mGBA"
        assert len(result.available_cores) == 1

    def test_platform_slug_passed_through(self):
        """platform_slug comes from the argument, not the bios dict."""
        bios = {"server_count": 1, "local_count": 1, "all_downloaded": True}
        result = format_bios_status(bios, "snes")
        assert result.platform_slug == "snes"

    def test_all_downloaded_true(self):
        """all_downloaded=True is preserved."""
        bios = {"all_downloaded": True, "server_count": 2, "local_count": 2}
        result = format_bios_status(bios, "psx")
        assert result.all_downloaded is True


class TestFormatBiosStatusMinimalDict:
    """Test with a minimal/empty bios dict — verify defaults."""

    def test_empty_dict_returns_defaults(self):
        """Missing optional fields fall back to defaults."""
        result = format_bios_status({}, "n64")

        assert isinstance(result, BiosStatus)
        assert result.platform_slug == "n64"
        assert result.server_count == 0
        assert result.local_count == 0
        assert result.all_downloaded is False
        assert result.required_count is None
        assert result.required_downloaded is None
        assert result.files == ()
        assert result.active_core is None
        assert result.active_core_label is None
        assert result.available_cores == ()

    def test_partial_dict_uses_provided_values(self):
        """Only provided keys are used; missing ones use defaults."""
        bios = {"server_count": 5, "local_count": 3}
        result = format_bios_status(bios, "gba")

        assert result.server_count == 5
        assert result.local_count == 3
        assert result.required_count is None
        assert result.files == ()

    def test_none_values_not_substituted(self):
        """Explicit None values in bios dict are returned as-is for optional fields."""
        bios = {"required_count": None, "required_downloaded": None}
        result = format_bios_status(bios, "gba")
        assert result.required_count is None
        assert result.required_downloaded is None


class TestFormatBiosStatusNeedsBiosContext:
    """Test that the function is caller-agnostic about needs_bios."""

    def test_function_formats_regardless_of_needs_bios(self):
        """format_bios_status does not check needs_bios — caller decides when to call it."""
        bios = {
            "needs_bios": False,
            "server_count": 0,
            "local_count": 0,
        }
        result = format_bios_status(bios, "gb")
        # Result is still returned — caller is responsible for the needs_bios guard
        assert result.platform_slug == "gb"
        assert result.server_count == 0

    def test_needs_bios_key_not_in_output(self):
        """needs_bios is not included in the returned BiosStatus."""
        bios = {"needs_bios": True, "server_count": 1}
        result = format_bios_status(bios, "gba")
        assert isinstance(result, BiosStatus)
        assert not hasattr(result, "needs_bios")


class TestClassifyFirmwareFile:
    """Tests for classify_firmware_file pure function."""

    def test_active_core_with_per_core_entry_required(self):
        """Active core that requires the file returns required=True."""
        reg_entry = {
            "description": "GBA BIOS",
            "required": True,
            "cores": {"gpsp_libretro": {"required": True}, "mgba_libretro": {"required": False}},
        }
        is_required, classification, description = classify_firmware_file(reg_entry, "gba_bios.bin", "gpsp_libretro")
        assert is_required is True
        assert classification == "required"
        assert description == "GBA BIOS"

    def test_active_core_with_per_core_entry_optional(self):
        """Active core that marks file optional returns required=False."""
        reg_entry = {
            "description": "GBA BIOS",
            "required": True,
            "cores": {"gpsp_libretro": {"required": True}, "mgba_libretro": {"required": False}},
        }
        is_required, classification, description = classify_firmware_file(reg_entry, "gba_bios.bin", "mgba_libretro")
        assert is_required is False
        assert classification == "optional"
        assert description == "GBA BIOS"

    def test_active_core_not_in_cores_dict(self):
        """Active core absent from cores dict yields required=False."""
        reg_entry = {
            "description": "Some BIOS",
            "required": True,
            "cores": {"known_core": {"required": True}},
        }
        is_required, classification, _ = classify_firmware_file(reg_entry, "some.bin", "unknown_core")
        assert is_required is False
        assert classification == "optional"

    def test_no_active_core_uses_toplevel_required(self):
        """Without active core, top-level required value is used."""
        reg_entry = {"description": "DC BIOS", "required": True}
        is_required, classification, description = classify_firmware_file(reg_entry, "dc_boot.bin", None)
        assert is_required is True
        assert classification == "required"
        assert description == "DC BIOS"

    def test_no_active_core_toplevel_optional(self):
        """Without active core, top-level required=False yields optional."""
        reg_entry = {"description": "DC Flash", "required": False}
        is_required, classification, _ = classify_firmware_file(reg_entry, "dc_flash.bin", None)
        assert is_required is False
        assert classification == "optional"

    def test_no_reg_entry_yields_unknown(self):
        """File not in registry yields unknown classification."""
        is_required, classification, description = classify_firmware_file(None, "mystery.bin", None)
        assert is_required is False
        assert classification == "unknown"
        assert description == "mystery.bin"

    def test_no_reg_entry_with_active_core_yields_unknown(self):
        """File not in registry with active core still yields unknown."""
        is_required, classification, description = classify_firmware_file(None, "alien.bin", "some_core")
        assert is_required is False
        assert classification == "unknown"
        assert description == "alien.bin"

    def test_description_falls_back_to_file_name(self):
        """reg_entry without description falls back to file_name."""
        reg_entry = {"required": True}
        _, _, description = classify_firmware_file(reg_entry, "bios.bin", None)
        assert description == "bios.bin"


class TestBuildCoresInfo:
    """Tests for build_cores_info pure function."""

    def test_with_cores_data_returns_formatted_dict(self):
        """reg_entry with cores key produces per-core required dict."""
        reg_entry = {
            "cores": {
                "mgba_libretro": {"required": False},
                "gpsp_libretro": {"required": True},
            }
        }
        result = build_cores_info(reg_entry)
        assert result == {
            "mgba_libretro": {"required": False},
            "gpsp_libretro": {"required": True},
        }

    def test_no_reg_entry_returns_empty_dict(self):
        """None reg_entry returns empty dict."""
        assert build_cores_info(None) == {}

    def test_reg_entry_without_cores_key_returns_empty_dict(self):
        """reg_entry missing cores key returns empty dict."""
        reg_entry = {"description": "Some BIOS", "required": True}
        assert build_cores_info(reg_entry) == {}

    def test_core_missing_required_defaults_to_true(self):
        """Core entry without required key defaults to True."""
        reg_entry = {"cores": {"some_core": {}}}
        result = build_cores_info(reg_entry)
        assert result["some_core"]["required"] is True


class TestIsUsedByActiveCore:
    """Tests for is_used_by_active_core pure function."""

    def test_no_active_core_returns_true(self):
        """No active core — file is considered used by all."""
        reg_entry = {"cores": {"mgba_libretro": {"required": False}}}
        assert is_used_by_active_core(reg_entry, None) is True

    def test_no_reg_entry_returns_true(self):
        """No registry entry — unknown file, considered used."""
        assert is_used_by_active_core(None, "mgba_libretro") is True

    def test_reg_entry_without_cores_returns_true(self):
        """Registry entry without cores key — file used by all cores."""
        reg_entry = {"description": "DC BIOS", "required": True}
        assert is_used_by_active_core(reg_entry, "some_core") is True

    def test_active_core_in_cores_returns_true(self):
        """Active core present in cores dict — file is used by it."""
        reg_entry = {"cores": {"mgba_libretro": {"required": False}}}
        assert is_used_by_active_core(reg_entry, "mgba_libretro") is True

    def test_active_core_not_in_cores_returns_false(self):
        """Active core not in cores dict — file not used by it."""
        reg_entry = {"cores": {"gpsp_libretro": {"required": True}}}
        assert is_used_by_active_core(reg_entry, "mgba_libretro") is False


class TestBuildFileEntry:
    """Tests for build_file_entry pure function."""

    def test_full_entry_with_reg_entry(self):
        """Full entry is built correctly when reg_entry is present."""
        reg_entry = {
            "description": "Dreamcast BIOS",
            "required": True,
            "cores": {"dc_libretro": {"required": True}},
        }
        result = build_file_entry("dc_boot.bin", True, "/bios/dc/dc_boot.bin", reg_entry, None)
        assert isinstance(result, BiosFileEntry)
        assert result.file_name == "dc_boot.bin"
        assert result.downloaded is True
        assert result.local_path == "/bios/dc/dc_boot.bin"
        assert result.required is True
        assert result.description == "Dreamcast BIOS"
        assert result.classification == "required"
        assert result.cores == {"dc_libretro": {"required": True}}
        assert result.used_by_active is True

    def test_no_reg_entry_yields_unknown(self):
        """Without reg_entry, classification is unknown and required is False."""
        result = build_file_entry("mystery.bin", False, "/bios/mystery.bin", None, None)
        assert isinstance(result, BiosFileEntry)
        assert result.file_name == "mystery.bin"
        assert result.downloaded is False
        assert result.required is False
        assert result.classification == "unknown"
        assert result.description == "mystery.bin"
        assert result.cores == {}
        assert result.used_by_active is True

    def test_downloaded_false_reflected(self):
        """downloaded=False is reflected in the entry."""
        reg_entry = {"description": "BIOS", "required": True}
        result = build_file_entry("bios.bin", False, "/bios/bios.bin", reg_entry, None)
        assert result.downloaded is False

    def test_downloaded_true_reflected(self):
        """downloaded=True is reflected in the entry."""
        reg_entry = {"description": "BIOS", "required": True}
        result = build_file_entry("bios.bin", True, "/bios/bios.bin", reg_entry, None)
        assert result.downloaded is True

    def test_active_core_not_in_cores_marks_not_used(self):
        """File with cores dict where active_core is absent has used_by_active=False."""
        reg_entry = {
            "description": "GBA BIOS",
            "required": True,
            "cores": {"gpsp_libretro": {"required": True}},
        }
        result = build_file_entry("gba_bios.bin", False, "/bios/gba_bios.bin", reg_entry, "mgba_libretro")
        assert result.used_by_active is False
        assert result.required is False
        assert result.classification == "optional"


class TestCollectFirmwareStatus:
    """Tests for collect_firmware_status pure function."""

    def test_multiple_items_mix_of_registered_and_unknown(self):
        """Mix of known and unknown files produces correct entries."""
        registry_platform = {
            "known.bin": {"description": "Known BIOS", "required": True},
        }
        items = [
            {"file_name": "known.bin", "downloaded": True, "dest": "/bios/known.bin"},
            {"file_name": "unknown.bin", "downloaded": False, "dest": "/bios/unknown.bin"},
        ]
        result = collect_firmware_status(items, registry_platform, None)
        assert len(result) == 2
        assert isinstance(result, tuple)
        assert all(isinstance(f, BiosFileEntry) for f in result)

        known = next(f for f in result if f.file_name == "known.bin")
        unknown = next(f for f in result if f.file_name == "unknown.bin")

        assert known.classification == "required"
        assert known.downloaded is True
        assert unknown.classification == "unknown"
        assert unknown.downloaded is False

    def test_empty_items_returns_empty_tuple(self):
        """No items produces empty result."""
        result = collect_firmware_status([], {"some.bin": {"required": True}}, None)
        assert result == ()

    def test_registry_platform_lookup_by_file_name(self):
        """reg_entry is looked up from registry_platform by file_name."""
        registry_platform = {
            "bios.bin": {"description": "My BIOS", "required": False},
        }
        items = [{"file_name": "bios.bin", "downloaded": False, "dest": "/bios/bios.bin"}]
        result = collect_firmware_status(items, registry_platform, None)
        assert result[0].classification == "optional"
        assert result[0].description == "My BIOS"

    def test_active_core_forwarded_to_classify(self):
        """active_core_so is passed through to per-core classification."""
        registry_platform = {
            "gba_bios.bin": {
                "description": "GBA BIOS",
                "required": True,
                "cores": {"mgba_libretro": {"required": False}},
            }
        }
        items = [{"file_name": "gba_bios.bin", "downloaded": False, "dest": "/bios/gba_bios.bin"}]
        result = collect_firmware_status(items, registry_platform, "mgba_libretro")
        assert result[0].required is False
        assert result[0].classification == "optional"


def _make_bios(**overrides) -> BiosStatus:
    defaults = {
        "platform_slug": "gba",
        "server_count": 3,
        "local_count": 2,
        "all_downloaded": False,
        "required_count": None,
        "required_downloaded": None,
        "files": (),
        "active_core": None,
        "active_core_label": None,
        "available_cores": (),
    }
    defaults.update(overrides)
    return BiosStatus(**defaults)


class TestComputeBiosLevel:
    def test_required_all_downloaded(self):
        assert compute_bios_level(_make_bios(required_count=2, required_downloaded=2)) == "ok"

    def test_required_partial(self):
        assert compute_bios_level(_make_bios(required_count=3, required_downloaded=1)) == "partial"

    def test_required_none_downloaded(self):
        assert compute_bios_level(_make_bios(required_count=2, required_downloaded=0)) == "missing"

    def test_no_required_all_downloaded(self):
        assert compute_bios_level(_make_bios(all_downloaded=True)) == "ok"

    def test_no_required_some_downloaded(self):
        assert compute_bios_level(_make_bios(local_count=1, all_downloaded=False)) == "partial"

    def test_no_required_none_downloaded(self):
        assert compute_bios_level(_make_bios(local_count=0, all_downloaded=False)) == "missing"


class TestComputeBiosLabel:
    def test_required_all_downloaded(self):
        assert compute_bios_label(_make_bios(required_count=2, required_downloaded=2)) == "OK"

    def test_required_partial(self):
        assert compute_bios_label(_make_bios(required_count=3, required_downloaded=1)) == "1/3 required"

    def test_required_none_downloaded(self):
        assert compute_bios_label(_make_bios(required_count=2, required_downloaded=0)) == "Missing"

    def test_no_required_all_downloaded(self):
        assert compute_bios_label(_make_bios(all_downloaded=True)) == "OK"

    def test_no_required_some_downloaded(self):
        assert compute_bios_label(_make_bios(server_count=5, local_count=3, all_downloaded=False)) == "3/5"

    def test_no_required_none_downloaded(self):
        assert compute_bios_label(_make_bios(local_count=0, all_downloaded=False)) == "Missing"


class TestAvailableCore:
    def test_construction(self):
        core = AvailableCore(core_so="mgba_libretro", label="mGBA", is_default=True)
        assert core.core_so == "mgba_libretro"
        assert core.label == "mGBA"
        assert core.is_default is True

    def test_frozen(self):
        core = AvailableCore(core_so="mgba_libretro", label="mGBA", is_default=True)
        try:
            core.label = "other"  # type: ignore[misc]
            raise AssertionError("Should have raised AttributeError")
        except AttributeError:
            pass

    def test_asdict(self):
        core = AvailableCore(core_so="mgba_libretro", label="mGBA", is_default=True)
        d = asdict(core)
        assert d == {"core_so": "mgba_libretro", "label": "mGBA", "is_default": True}


class TestBiosFileEntry:
    def test_construction(self):
        entry = BiosFileEntry(
            file_name="gba_bios.bin",
            downloaded=True,
            local_path="/bios/gba_bios.bin",
            required=True,
            description="GBA BIOS",
            classification="required",
            cores={"mgba_libretro": {"required": True}},
            used_by_active=True,
        )
        assert entry.file_name == "gba_bios.bin"
        assert entry.downloaded is True
        assert entry.classification == "required"

    def test_asdict(self):
        entry = BiosFileEntry(
            file_name="gba_bios.bin",
            downloaded=False,
            local_path="/bios/gba_bios.bin",
            required=False,
            description="GBA BIOS",
            classification="optional",
            cores={},
            used_by_active=False,
        )
        d = asdict(entry)
        assert d["file_name"] == "gba_bios.bin"
        assert d["downloaded"] is False
        assert d["cores"] == {}


class TestBiosStatus:
    def _make_status(self, **overrides):
        defaults = {
            "platform_slug": "gba",
            "server_count": 3,
            "local_count": 2,
            "all_downloaded": False,
            "required_count": 2,
            "required_downloaded": 1,
            "files": (),
            "active_core": "mgba_libretro",
            "active_core_label": "mGBA",
            "available_cores": (),
        }
        defaults.update(overrides)
        return BiosStatus(**defaults)

    def test_construction(self):
        status = self._make_status()
        assert status.platform_slug == "gba"
        assert status.server_count == 3
        assert status.cached_at == 0.0

    def test_cached_at_override(self):
        status = self._make_status(cached_at=1234.5)
        assert status.cached_at == 1234.5

    def test_none_optional_fields(self):
        status = self._make_status(required_count=None, required_downloaded=None, active_core=None)
        assert status.required_count is None
        assert status.active_core is None

    def test_asdict_roundtrip(self):
        core = AvailableCore(core_so="mgba_libretro", label="mGBA", is_default=True)
        file_entry = BiosFileEntry(
            file_name="gba_bios.bin",
            downloaded=True,
            local_path="/bios/gba_bios.bin",
            required=True,
            description="GBA BIOS",
            classification="required",
            cores={"mgba_libretro": {"required": True}},
            used_by_active=True,
        )
        status = self._make_status(files=(file_entry,), available_cores=(core,))
        d = asdict(status)
        assert d["platform_slug"] == "gba"
        assert len(d["files"]) == 1
        assert d["files"][0]["file_name"] == "gba_bios.bin"
        assert len(d["available_cores"]) == 1
        assert d["available_cores"][0]["core_so"] == "mgba_libretro"
