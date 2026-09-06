"""Tests for the atlas catalogue adapter — the translation into plugin vocabulary.

What is under test is the adapter's own work: recovering the DECLARED order from
an answer stated in effective order, classifying each entry through the plugin's
own bake kernel, telling the five catalogue refusals apart from a frontend that
genuinely declares no emulator, and refusing to turn any failure into an empty
list. The resolver's own decisions are upstream's and are not re-tested here.

Answers are built from real atlas value objects rather than mocks, so the field
names and shapes under test are the resolver's own and a rename upstream fails
here rather than passing against a stand-in. It is not a guarantee that a fixture
resembles a real machine: most of these shapes carry no validation, and some are
assembled deliberately — a derived entry beside a declared one, say — to isolate
one rule.

The last class is the exception: it drives the REAL resolver over a fabricated
RetroDECK deploy under ``tmp_path``, which is the only way to reach the answers
a malformed catalogue produces.
"""

from __future__ import annotations

import json
import os
from typing import Any, cast

import pytest
from _vendor.atlas import (
    CAVEAT_EMULATOR_CATALOGUE_EXCLUSIVE,
    CAVEAT_EMULATOR_CATALOGUE_SEALED,
    CAVEAT_EMULATOR_CATALOGUE_UNAVAILABLE,
    CAVEAT_EMULATOR_CATALOGUE_UNESTABLISHED,
    CAVEAT_EMULATOR_CATALOGUE_UNREADABLE,
    CAVEAT_EMULATOR_LIST_DERIVED,
    HEALTH_ISSUE_CATALOGUE_INVALID,
    HEALTH_ISSUE_ROOT_MISSING,
    KIND_LIBRETRO,
    KIND_STANDALONE,
)
from _vendor.atlas.esde import EmulatorSpec
from _vendor.atlas.installations import CatalogueAnswer, EmulatorEntry, RomPlacement, SystemsAnswer
from _vendor.atlas.placement import Caveat

from adapters.atlas_catalogue import AtlasCatalogueAdapter, first_detected_installation
from domain.shortcut_data import EmulatorInvocation

_RETROARCH = "%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/{core}.so %ROM%"

_HEALTH = Caveat(code=HEALTH_ISSUE_ROOT_MISSING, message="a health finding, on every answer this machine gives")


def _entry(
    *,
    label: str,
    command: str,
    declared_index: int | None,
    kind: str = KIND_STANDALONE,
    core_so: str | None = None,
    selection: str | None = None,
    system: str = "ps3",
) -> EmulatorEntry:
    """One catalogue entry, as the resolver hands it over.

    The host an entry is bound to answers its placement questions, and nothing
    here asks one — the adapter reads the spec's fields alone.
    """
    return EmulatorEntry(
        cast("Any", None),
        EmulatorSpec(
            system=system,
            label=label,
            kind=kind,
            core_so=core_so,
            command=command,
            provenance="bundled es_systems.xml",
            declared_index=declared_index,
            selection=selection,
        ),
    )


def _libretro(*, label: str, core: str, declared_index: int | None, selection: str | None = None) -> EmulatorEntry:
    return _entry(
        label=label,
        command=_RETROARCH.format(core=core),
        declared_index=declared_index,
        kind=KIND_LIBRETRO,
        core_so=f"{core}.so",
        selection=selection,
    )


def _answer(*entries: EmulatorEntry, caveats: tuple[Caveat, ...] = ()) -> CatalogueAnswer:
    return CatalogueAnswer(entries=entries, sources=("es_systems.xml",), caveats=caveats)


def _refusal(code: str) -> Caveat:
    return Caveat(code=code, message="stated by the resolver")


class _Installation:
    """Stand-in for a detected installation handle — answers what it was prepared with."""

    kind = "retrodeck"

    def __init__(
        self,
        *,
        catalogue: CatalogueAnswer | Exception | None = None,
        placement: RomPlacement | Exception | None = None,
        systems: SystemsAnswer | Exception | None = None,
    ) -> None:
        self._catalogue = catalogue
        self._placement = placement
        self._systems = systems
        self.catalogue_calls: list[str] = []
        self.placement_calls: list[str] = []
        self.systems_calls = 0

    def emulators_for(self, system: str, *, content_path: str | None = None) -> CatalogueAnswer:
        self.catalogue_calls.append(system)
        return _answered(self._catalogue, _answer())

    def rom_location(self, system: str) -> RomPlacement:
        self.placement_calls.append(system)
        return _answered(self._placement, RomPlacement())

    def systems(self) -> SystemsAnswer:
        self.systems_calls += 1
        return _answered(self._systems, SystemsAnswer())


def _answered(prepared: Any, fallback: Any) -> Any:
    if isinstance(prepared, Exception):
        raise prepared
    return fallback if prepared is None else prepared


@pytest.fixture
def traces() -> list[str]:
    return []


def _adapter(
    installation: _Installation | None,
    traces: list[str],
    *,
    installed: bool = True,
) -> AtlasCatalogueAdapter:
    return AtlasCatalogueAdapter(
        choose_installation=lambda: installation,
        emulator_installed=lambda command: installed,
        log_debug=traces.append,
    )


def _labels(adapter: AtlasCatalogueAdapter, system: str = "ps3") -> list[str]:
    return [option.label for option in adapter.get_emulator_options(system)["options"]]


class TestDeclaredOrder:
    """ES-DE's own selections never move the plugin's default (ADR-0012)."""

    def test_a_promoted_entry_does_not_become_the_default(self, traces):
        # The answer is in EFFECTIVE order: the user's gamelist promotion put the
        # entry declared fourth at the front.
        installation = _Installation(
            catalogue=_answer(
                _entry(
                    label="RPCS3 Directory",
                    command="%EMULATOR_RPCS3% --no-gui %ROM%",
                    declared_index=3,
                    selection="gamelist <alternativeEmulator>",
                ),
                _entry(label="RPCS3", command="%EMULATOR_RPCS3% %ROM%", declared_index=0),
                _entry(label="RPCS3 Firmware", command="%EMULATOR_RPCS3% --installfw %ROM%", declared_index=1),
            )
        )
        adapter = _adapter(installation, traces)

        assert _labels(adapter) == ["RPCS3", "RPCS3 Firmware", "RPCS3 Directory"]
        assert adapter.get_default_emulator("ps3") == EmulatorInvocation.standalone("%EMULATOR_RPCS3% %ROM%", "RPCS3")

    def test_the_promoted_entry_is_marked_default_when_it_is_also_declared_first(self, traces):
        installation = _Installation(
            catalogue=_answer(
                _entry(
                    label="RPCS3",
                    command="%EMULATOR_RPCS3% %ROM%",
                    declared_index=0,
                    selection="gamelist <alternativeEmulator>",
                ),
                _entry(label="RPCS3 Directory", command="%EMULATOR_RPCS3% --no-gui %ROM%", declared_index=1),
            )
        )
        adapter = _adapter(installation, traces)

        assert _labels(adapter) == ["RPCS3", "RPCS3 Directory"]
        assert adapter.get_default_emulator("ps3") == EmulatorInvocation.standalone("%EMULATOR_RPCS3% %ROM%", "RPCS3")

    def test_a_skipped_declared_position_is_ordered_not_indexed(self, traces):
        # ES-DE's own walk skips a position where an empty-text <command> holds
        # one, so the values are ascending but need not start at 0 or be dense.
        installation = _Installation(
            catalogue=_answer(
                _entry(label="Third", command="%EMULATOR_C% %ROM%", declared_index=5),
                _entry(label="First", command="%EMULATOR_A% %ROM%", declared_index=1),
                _entry(label="Second", command="%EMULATOR_B% %ROM%", declared_index=4),
            )
        )
        assert _labels(_adapter(installation, traces)) == ["First", "Second", "Third"]

    def test_an_entry_with_no_declared_position_is_never_the_default(self, traces):
        # The derived enumeration: no layer declared these, so none has a shipped
        # position — and each carries an empty command, which is unbakeable.
        installation = _Installation(
            catalogue=_answer(
                _entry(label="mGBA", command="", declared_index=None, kind=KIND_LIBRETRO, core_so="mgba_libretro.so"),
                _entry(label="VBA-M", command="", declared_index=None, kind=KIND_LIBRETRO, core_so="vbam_libretro.so"),
                caveats=(Caveat(code=CAVEAT_EMULATOR_LIST_DERIVED, message="derived from the installed cores"),),
            )
        )
        adapter = _adapter(installation, traces)

        result = adapter.get_emulator_options("gba")
        assert result["available"] is True
        assert [option.label for option in result["options"]] == ["mGBA", "VBA-M"]
        assert all(option.status == "unbakeable" for option in result["options"])
        assert adapter.get_default_emulator("gba") is None

    def test_an_undeclared_entry_sorts_behind_every_declared_one(self, traces):
        installation = _Installation(
            catalogue=_answer(
                _entry(label="Derived", command="", declared_index=None),
                _entry(label="Declared", command="%EMULATOR_A% %ROM%", declared_index=0),
            )
        )
        assert _labels(_adapter(installation, traces)) == ["Declared", "Derived"]

    def test_an_undeclared_entry_says_so_in_the_log(self, traces):
        # "No default" is not a bug in this one shape, so the log has to be able
        # to tell it apart from a default the plugin failed to find.
        installation = _Installation(
            catalogue=_answer(
                _entry(label="Derived", command="", declared_index=None),
                _entry(label="Declared", command="%EMULATOR_A% %ROM%", declared_index=0),
            )
        )
        _adapter(installation, traces).get_emulator_options("ps3")

        assert any("no declared position" in line and "Derived" in line for line in traces)

    def test_a_fully_declared_listing_says_nothing_about_undeclared_entries(self, traces):
        installation = _Installation(
            catalogue=_answer(_entry(label="Declared", command="%EMULATOR_A% %ROM%", declared_index=0))
        )
        _adapter(installation, traces).get_emulator_options("ps3")

        assert not any("no declared position" in line for line in traces)


class TestCatalogueRefusals:
    """Six ways to answer nothing, and only one is a statement about the machine.

    Five refusal codes, plus the catalogue that was read and declares no emulator
    for this system.
    """

    @pytest.mark.parametrize(
        "code",
        [
            CAVEAT_EMULATOR_CATALOGUE_UNAVAILABLE,
            CAVEAT_EMULATOR_CATALOGUE_UNESTABLISHED,
            CAVEAT_EMULATOR_CATALOGUE_UNREADABLE,
            CAVEAT_EMULATOR_CATALOGUE_SEALED,
            HEALTH_ISSUE_CATALOGUE_INVALID,
        ],
    )
    def test_a_refusal_answers_unavailable(self, traces, code):
        installation = _Installation(catalogue=_answer(caveats=(_refusal(code),)))
        assert _adapter(installation, traces).get_emulator_options("ps3") == {"available": False, "options": []}

    def test_a_refusal_suppresses_the_entries_it_arrived_with(self, traces):
        # `sealed` is the one refusal that may accompany real entries: what the
        # readable layers declare is stated, and the caveat says the rest may
        # declare more. The plugin cannot show a list it knows is partial as if
        # it were the whole one.
        installation = _Installation(
            catalogue=_answer(
                _entry(label="RPCS3", command="%EMULATOR_RPCS3% %ROM%", declared_index=0),
                caveats=(_refusal(CAVEAT_EMULATOR_CATALOGUE_SEALED),),
            )
        )
        adapter = _adapter(installation, traces)

        assert adapter.get_emulator_options("ps3") == {"available": False, "options": []}
        assert adapter.get_default_emulator("ps3") is None
        assert adapter.get_active_core("ps3") == (None, None)

    def test_an_empty_answer_without_a_refusal_is_a_real_none(self, traces):
        installation = _Installation(catalogue=_answer())
        assert _adapter(installation, traces).get_emulator_options("ps3") == {"available": True, "options": []}

    def test_a_health_finding_is_not_a_refusal(self, traces):
        # The test is the codes, never an empty caveat list: a broken
        # installation states its findings on every answer it gives.
        installation = _Installation(catalogue=_answer(caveats=(_HEALTH,)))
        assert _adapter(installation, traces).get_emulator_options("ps3") == {"available": True, "options": []}

    def test_an_exclusive_overlay_is_not_a_refusal(self, traces):
        # A custom es_systems.xml declaring itself the whole catalogue: the
        # answer is COMPLETE, and the code only says why it is as small as it is.
        installation = _Installation(
            catalogue=_answer(
                _entry(label="My PCSX2", command="%EMULATOR_PCSX2% -batch %ROM%", declared_index=0),
                caveats=(Caveat(code=CAVEAT_EMULATOR_CATALOGUE_EXCLUSIVE, message="a <loadExclusive/> overlay"),),
            )
        )
        adapter = _adapter(installation, traces)

        assert _labels(adapter) == ["My PCSX2"]
        assert adapter.get_default_emulator("ps3") == EmulatorInvocation.standalone(
            "%EMULATOR_PCSX2% -batch %ROM%", "My PCSX2"
        )

    def test_the_codes_reach_the_log(self, traces):
        installation = _Installation(catalogue=_answer(caveats=(_refusal(CAVEAT_EMULATOR_CATALOGUE_UNREADABLE),)))
        _adapter(installation, traces).get_emulator_options("ps3")
        assert any(CAVEAT_EMULATOR_CATALOGUE_UNREADABLE in line for line in traces)

    def test_the_codes_of_every_answer_reach_the_log(self, traces):
        # The resolver never logs, so a code that reaches no log reaches nobody
        # — and the catalogue is not the only answer that carries one.
        installation = _Installation(
            systems=SystemsAnswer(caveats=(_refusal(CAVEAT_EMULATOR_CATALOGUE_UNESTABLISHED),)),
            placement=RomPlacement(caveats=(_refusal(CAVEAT_EMULATOR_CATALOGUE_UNREADABLE),)),
        )
        adapter = _adapter(installation, traces)

        adapter.is_known_system("ps3")
        adapter.get_supported_extensions("ps3")

        assert any("systems" in line and CAVEAT_EMULATOR_CATALOGUE_UNESTABLISHED in line for line in traces)
        assert any("extensions" in line and CAVEAT_EMULATOR_CATALOGUE_UNREADABLE in line for line in traces)


class TestNothingToAsk:
    """A question nobody could answer is never an empty answer."""

    def test_no_installation_answers_unavailable(self, traces):
        adapter = _adapter(None, traces)

        assert adapter.get_emulator_options("ps3") == {"available": False, "options": []}
        assert adapter.get_default_emulator("ps3") is None
        assert adapter.get_active_core("ps3") == (None, None)
        assert adapter.is_known_system("ps3") is None
        assert adapter.get_supported_extensions("ps3") == frozenset()
        assert adapter.system_supports_m3u("ps3") is False
        assert any("no emulator installation detected" in line for line in traces)

    def test_a_raising_resolver_answers_unavailable(self, traces):
        installation = _Installation(catalogue=ValueError("an invariant of its own"))
        adapter = _adapter(installation, traces)

        assert adapter.get_emulator_options("ps3") == {"available": False, "options": []}
        assert adapter.get_active_core("ps3") == (None, None)
        assert any("resolver failed" in line for line in traces)

    def test_a_raising_detection_answers_unavailable(self, traces):
        def explode() -> Any:
            raise RuntimeError("detection blew up")

        adapter = AtlasCatalogueAdapter(
            choose_installation=explode,
            emulator_installed=lambda command: True,
            log_debug=traces.append,
        )
        assert adapter.get_emulator_options("ps3") == {"available": False, "options": []}

    def test_a_raising_systems_read_answers_neither_yes_nor_no(self, traces):
        installation = _Installation(systems=ValueError("an invariant of its own"))
        assert _adapter(installation, traces).is_known_system("ps3") is None

    def test_a_raising_location_read_answers_the_default_safe_empty(self, traces):
        installation = _Installation(placement=ValueError("an invariant of its own"))
        adapter = _adapter(installation, traces)

        assert adapter.get_supported_extensions("psx") == frozenset()
        assert adapter.system_supports_m3u("psx") is False


class TestActiveCore:
    """The libretro-only projection the firmware BIOS filter keys on."""

    def test_the_first_libretro_entry_in_declared_order_wins(self, traces):
        installation = _Installation(
            catalogue=_answer(
                _libretro(label="gpSP", core="gpsp_libretro", declared_index=2),
                _entry(label="mGBA Standalone", command="%EMULATOR_MGBA% %ROM%", declared_index=0),
                _libretro(label="mGBA", core="mgba_libretro", declared_index=1),
            )
        )
        assert _adapter(installation, traces).get_active_core("gba") == ("mgba_libretro", "mGBA")

    def test_a_promotion_does_not_move_the_active_core(self, traces):
        installation = _Installation(
            catalogue=_answer(
                _libretro(label="gpSP", core="gpsp_libretro", declared_index=2, selection="gamelist"),
                _libretro(label="mGBA", core="mgba_libretro", declared_index=1),
            )
        )
        assert _adapter(installation, traces).get_active_core("gba") == ("mgba_libretro", "mGBA")

    def test_a_standalone_only_system_has_no_active_core(self, traces):
        installation = _Installation(
            catalogue=_answer(_entry(label="RPCS3", command="%EMULATOR_RPCS3% %ROM%", declared_index=0))
        )
        assert _adapter(installation, traces).get_active_core("ps3") == (None, None)

    def test_a_libretro_command_the_plugin_cannot_bake_still_names_its_core(self, traces):
        # Bakeability is a fact about this plugin's -e override; which core a
        # command loads is a fact about the command, and the BIOS filter asks
        # the second question.
        installation = _Installation(
            catalogue=_answer(
                _entry(
                    label="MAME",
                    command='%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mame_libretro.so "%ROM%"',
                    declared_index=0,
                    kind=KIND_LIBRETRO,
                    core_so="mame_libretro.so",
                )
            )
        )
        adapter = _adapter(installation, traces)

        assert adapter.get_active_core("arcade") == ("mame_libretro", "MAME")
        assert adapter.get_emulator_options("arcade")["options"][0].status == "unbakeable"


class TestInstalledProbe:
    """The injected find-rules verdict reaches the classifier's downgrade rule."""

    def test_a_missing_standalone_is_downgraded_and_loses_the_default(self, traces):
        installation = _Installation(
            catalogue=_answer(
                _entry(label="Ryubing", command="%EMULATOR_RYUBING% %ROM%", declared_index=0),
                _libretro(label="Yuzu", core="yuzu_libretro", declared_index=1),
            )
        )
        adapter = _adapter(installation, traces, installed=False)

        options = adapter.get_emulator_options("switch")["options"]
        assert (options[0].status, options[0].reason) == ("needs_setup", "not_installed")
        assert adapter.get_default_emulator("switch") == EmulatorInvocation.libretro("yuzu_libretro", "Yuzu")

    def test_a_libretro_entry_is_never_downgraded(self, traces):
        installation = _Installation(catalogue=_answer(_libretro(label="mGBA", core="mgba_libretro", declared_index=0)))
        adapter = _adapter(installation, traces, installed=False)

        assert adapter.get_emulator_options("gba")["options"][0].status == "bakeable"

    def test_the_probe_is_re_run_on_every_call(self, traces):
        # The answer is cached; the on-disk verdict is not, so an emulator the
        # user installs mid-session is seen without a cache reset.
        installation = _Installation(
            catalogue=_answer(_entry(label="Ryubing", command="%EMULATOR_RYUBING% %ROM%", declared_index=0))
        )
        installed = {"value": False}
        adapter = AtlasCatalogueAdapter(
            choose_installation=lambda: installation,
            emulator_installed=lambda command: installed["value"],
            log_debug=traces.append,
        )

        assert adapter.get_emulator_options("switch")["options"][0].status == "needs_setup"
        installed["value"] = True
        assert adapter.get_emulator_options("switch")["options"][0].status == "bakeable"


class TestAcceptList:
    def test_the_declared_extensions_arrive_lowercased(self, traces):
        installation = _Installation(placement=RomPlacement(dir="/roms/psx", extensions=(".CUE", ".chd", ".M3U")))
        adapter = _adapter(installation, traces)

        assert adapter.get_supported_extensions("psx") == frozenset({".cue", ".chd", ".m3u"})
        assert adapter.system_supports_m3u("psx") is True

    def test_a_system_without_m3u_says_so(self, traces):
        installation = _Installation(placement=RomPlacement(dir="/roms/switch", extensions=(".nsp", ".xci")))
        assert _adapter(installation, traces).system_supports_m3u("switch") is False

    def test_an_unresolved_location_answers_the_default_safe_empty(self, traces):
        installation = _Installation(placement=RomPlacement(caveats=(_refusal(CAVEAT_EMULATOR_CATALOGUE_UNREADABLE),)))
        assert _adapter(installation, traces).get_supported_extensions("psx") == frozenset()


class TestKnownSystem:
    def test_a_listed_system_is_known(self, traces):
        installation = _Installation(systems=SystemsAnswer(systems=("psx", "ps2", "ps3")))
        assert _adapter(installation, traces).is_known_system("ps2") is True

    def test_a_system_the_catalogue_does_not_name_is_a_positive_no(self, traces):
        installation = _Installation(systems=SystemsAnswer(systems=("psx", "ps2")))
        assert _adapter(installation, traces).is_known_system("nintendo64") is False

    def test_a_refused_catalogue_answers_none_rather_than_no(self, traces):
        installation = _Installation(systems=SystemsAnswer(caveats=(_refusal(CAVEAT_EMULATOR_CATALOGUE_UNREADABLE),)))
        assert _adapter(installation, traces).is_known_system("ps2") is None


class TestCaching:
    def test_one_answer_per_system_is_read_once(self, traces):
        installation = _Installation(
            catalogue=_answer(_entry(label="RPCS3", command="%EMULATOR_RPCS3% %ROM%", declared_index=0)),
            placement=RomPlacement(dir="/roms/ps3", extensions=(".ps3",)),
        )
        adapter = _adapter(installation, traces)

        for _ in range(3):
            adapter.get_emulator_options("ps3")
            adapter.get_active_core("ps3")
            adapter.get_supported_extensions("ps3")

        assert installation.catalogue_calls == ["ps3"]
        assert installation.placement_calls == ["ps3"]

    def test_each_system_is_asked_for_itself(self, traces):
        installation = _Installation()
        adapter = _adapter(installation, traces)

        adapter.get_emulator_options("ps3")
        adapter.get_emulator_options("psx")

        assert installation.catalogue_calls == ["ps3", "psx"]

    def test_the_systems_listing_is_read_once(self, traces):
        # The listing enumerates every system the catalogue has, and a candidate
        # search asks it once per platform it visits.
        installation = _Installation(systems=SystemsAnswer(systems=("psx", "ps2")))
        adapter = _adapter(installation, traces)

        adapter.is_known_system("psx")
        adapter.is_known_system("ps2")
        adapter.is_known_system("n64")

        assert installation.systems_calls == 1

    def test_reset_cache_re_asks(self, traces):
        installation = _Installation(
            catalogue=_answer(_entry(label="RPCS3", command="%EMULATOR_RPCS3% %ROM%", declared_index=0)),
            placement=RomPlacement(dir="/roms/ps3", extensions=(".ps3",)),
            systems=SystemsAnswer(systems=("ps3",)),
        )
        adapter = _adapter(installation, traces)
        adapter.get_emulator_options("ps3")
        adapter.get_supported_extensions("ps3")
        adapter.is_known_system("ps3")

        adapter.reset_cache()
        adapter.get_emulator_options("ps3")
        adapter.get_supported_extensions("ps3")
        adapter.is_known_system("ps3")

        assert installation.catalogue_calls == ["ps3", "ps3"]
        assert installation.placement_calls == ["ps3", "ps3"]
        assert installation.systems_calls == 2

    def test_a_detection_that_found_nothing_is_re_run(self, traces):
        # The one state where memoising would cost something real: RetroDECK
        # installed mid-session must be picked up without a plugin reload.
        found: list[_Installation | None] = [None]
        adapter = AtlasCatalogueAdapter(
            choose_installation=lambda: found[0],
            emulator_installed=lambda command: True,
            log_debug=traces.append,
        )

        assert adapter.get_emulator_options("ps3")["available"] is False
        found[0] = _Installation(
            catalogue=_answer(_entry(label="RPCS3", command="%EMULATOR_RPCS3% %ROM%", declared_index=0))
        )
        assert _labels(adapter) == ["RPCS3"]

    def test_a_chosen_installation_is_asked_for_once(self, traces):
        installation = _Installation()
        chooses: list[int] = []

        def choose() -> _Installation:
            chooses.append(1)
            return installation

        adapter = AtlasCatalogueAdapter(
            choose_installation=choose,
            emulator_installed=lambda command: True,
            log_debug=traces.append,
        )
        adapter.get_emulator_options("ps3")
        adapter.get_emulator_options("psx")
        adapter.is_known_system("ps3")

        assert len(chooses) == 1


# --- The real resolver, over a fabricated RetroDECK deploy -------------------

_LINUX_SYSTEMS_SUFFIX = os.path.join(
    "retrodeck", "components", "es-de", "share", "es-de", "resources", "systems", "linux", "es_systems.xml"
)

_VALID_ES_SYSTEMS_XML = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>gba</name>
    <extension>.gba</extension>
    <command label="mGBA">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mgba_libretro.so %ROM%</command>
  </system>
</systemList>
"""


def _seed_retrodeck(tmp_path, *, catalogue: str) -> str:
    """Lay down a RetroDECK marker and a bundled catalogue; return the home."""
    home = tmp_path / "home"
    marker = home / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck" / "retrodeck.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"paths": {"rd_home_path": str(home / "retrodeck")}}), encoding="utf-8")

    deploy = home / ".local" / "share" / "flatpak" / "app" / "net.retrodeck.retrodeck" / "current" / "active" / "files"
    bundled = deploy / _LINUX_SYSTEMS_SUFFIX
    bundled.parent.mkdir(parents=True, exist_ok=True)
    bundled.write_text(catalogue, encoding="utf-8")
    return str(home)


@pytest.fixture(autouse=True)
def _no_host_deploy(tmp_path, monkeypatch):
    """Never resolve ``/app`` out of the dev box's own RetroDECK deploy.

    The resolver finds the flatpak deploy through its own module-level constants
    — the user base under the given home, then the system one — so a tmp home
    with a marker and no seeded deploy answers off the host unless the system
    constant is repointed. Same reasoning, and the same accepted cost of naming a
    private vendored symbol, as ``tests/contract/conftest.py``.
    """
    monkeypatch.setattr(
        "_vendor.atlas.installations._FLATPAK_DEPLOY_SYSTEM", str(tmp_path / "no_system_flatpak" / "app")
    )


class TestTheRealResolverOverARealTree:
    """The adapter against the real resolver, not a stand-in installation.

    Successors to the deleted parser's ``test_unavailable_when_parse_fails`` and
    ``test_wrong_root_tag_returns_empty``: ES-DE refuses its whole load on either
    file, so the resolver truthfully enumerates nothing — and the plugin must not
    render that as "this frontend knows no emulator".
    """

    def _adapter(self, home: str, traces: list[str]) -> AtlasCatalogueAdapter:
        return AtlasCatalogueAdapter(
            choose_installation=lambda: first_detected_installation(home),
            emulator_installed=lambda command: True,
            log_debug=traces.append,
        )

    def test_a_readable_catalogue_answers_from_the_seeded_tree(self, tmp_path, traces):
        # The control for the two unavailable cases below: without it, their
        # assertions would also pass on a tree the resolver never found at all.
        adapter = self._adapter(_seed_retrodeck(tmp_path, catalogue=_VALID_ES_SYSTEMS_XML), traces)

        assert _labels(adapter, "gba") == ["mGBA"]
        assert adapter.get_active_core("gba") == ("mgba_libretro", "mGBA")
        assert adapter.is_known_system("gba") is True
        assert adapter.get_supported_extensions("gba") == frozenset({".gba"})

    def test_a_catalogue_that_does_not_parse_is_unavailable(self, tmp_path, traces):
        home = _seed_retrodeck(tmp_path, catalogue="<systemList><system><name>gba</name>")
        adapter = self._adapter(home, traces)

        assert adapter.get_emulator_options("gba") == {"available": False, "options": []}
        assert adapter.get_default_emulator("gba") is None
        assert adapter.get_active_core("gba") == (None, None)
        assert adapter.is_known_system("gba") is None

    def test_a_wrong_root_tag_is_unavailable(self, tmp_path, traces):
        home = _seed_retrodeck(tmp_path, catalogue='<?xml version="1.0"?>\n<notSystemList/>\n')
        adapter = self._adapter(home, traces)

        assert adapter.get_emulator_options("gba") == {"available": False, "options": []}
        assert adapter.is_known_system("gba") is None

    def test_an_empty_but_valid_catalogue_is_a_real_knows_none(self, tmp_path, traces):
        # The boundary of the case above: a document ES-DE loads fine that simply
        # declares no system is a statement about the machine, not a failure.
        home = _seed_retrodeck(tmp_path, catalogue='<?xml version="1.0"?>\n<systemList/>\n')
        adapter = self._adapter(home, traces)

        assert adapter.get_emulator_options("gba") == {"available": True, "options": []}
        assert adapter.is_known_system("gba") is False

    def test_nothing_detected_answers_unavailable(self, tmp_path, traces):
        adapter = self._adapter(str(tmp_path / "empty-home"), traces)

        assert adapter.get_emulator_options("gba") == {"available": False, "options": []}
        assert adapter.is_known_system("gba") is None
