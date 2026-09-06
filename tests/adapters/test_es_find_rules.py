"""Tests for adapters/es_find_rules — where an emulator's binary lives on this machine.

Drives the real ``find_es_find_rules_xml`` → parse → on-disk probe path over a
fabricated per-user flatpak tree, so the sandbox ``/app`` and ``/var/data``
prefix mappings are validated against real files rather than against a mock of
the resolution seam.
"""

import logging
import os
from unittest import mock

import pytest

from adapters.es_find_rules import EsFindRulesAdapter, emulator_token

# conftest.py patches decky before this import.
# main.py adds py_modules to sys.path (provides vdf, etc.).
from main import Plugin  # noqa: F401

_TEST_LOGGER = logging.getLogger("test_es_find_rules")

# Mixes the shapes the probe must reason about: a RetroDECK component with both a
# bundled (/app) and an external (/var/data) staticpath, a systempath-only
# emulator with no staticpath at all (unverifiable from outside the sandbox →
# assumed installed), a host-only entry, and a ``|command``-suffixed entry.
_FIND_RULES_XML = """\
<?xml version="1.0"?>
<ruleList>
  <emulator name="RPCS3">
    <rule type="systempath">
      <entry>rpcs3</entry>
    </rule>
    <rule type="staticpath">
      <entry>~/Applications/rpcs3*.AppImage</entry>
      <entry>/var/lib/flatpak/exports/bin/net.rpcs3.RPCS3</entry>
      <entry>/app/retrodeck/components/rpcs3/component_launcher.sh</entry>
    </rule>
  </emulator>
  <emulator name="RYUBING">
    <rule type="systempath">
      <entry>ryubing</entry>
    </rule>
    <rule type="staticpath">
      <entry>/app/retrodeck/components/ryubing/component_launcher.sh</entry>
      <entry>/var/data/retrodeck/external_components/ryubing/component_launcher.sh</entry>
    </rule>
  </emulator>
  <emulator name="PPSSPP">
    <rule type="staticpath">
      <entry>/app/retrodeck/components/ppsspp/component_launcher.sh</entry>
    </rule>
  </emulator>
  <emulator name="EXTONLY">
    <rule type="staticpath">
      <entry>/var/data/retrodeck/external_components/extonly/component_launcher.sh</entry>
    </rule>
  </emulator>
  <emulator name="HOSTONLY">
    <rule type="staticpath">
      <entry>~/Applications/hostonly*.AppImage</entry>
    </rule>
  </emulator>
  <emulator name="PIPED">
    <rule type="staticpath">
      <entry>/app/retrodeck/components/piped/component_launcher.sh|--flag %ROM%</entry>
    </rule>
  </emulator>
  <emulator name="ATARI800">
    <rule type="systempath">
      <entry>atari800</entry>
    </rule>
  </emulator>
</ruleList>
"""


def _user_files_dir(user_home):
    """The per-user flatpak app ``files`` dir for the RetroDECK app under *user_home*."""
    return (
        user_home / ".local" / "share" / "flatpak" / "app" / "net.retrodeck.retrodeck" / "current" / "active" / "files"
    )


def _find_rules_path(files_dir, *, flavor: str) -> str:
    """The ``es_find_rules.xml`` path for *flavor* under a flatpak app ``files`` dir."""
    return os.path.join(
        files_dir,
        "retrodeck",
        "components",
        "es-de",
        "share",
        "es-de",
        "resources",
        "systems",
        flavor,
        "es_find_rules.xml",
    )


def _component_launcher(files_dir, component: str) -> str:
    """Bundled RetroDECK component launcher path under the flatpak files tree."""
    return os.path.join(files_dir, "retrodeck", "components", component, "component_launcher.sh")


def _external_component_launcher(user_home, component: str) -> str:
    """User-installed external RetroDECK component launcher (sandbox ``/var/data``)."""
    return os.path.join(
        user_home,
        ".var",
        "app",
        "net.retrodeck.retrodeck",
        "data",
        "retrodeck",
        "external_components",
        component,
        "component_launcher.sh",
    )


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n")


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


@pytest.fixture(autouse=True)
def _isolate_system_root(tmp_path):
    with mock.patch("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", str(tmp_path / "nonexistent_system_root")):
        yield


def _seed(tmp_path, *, flavor: str = "linux", installed=(), external=(), find_rules: str | None = _FIND_RULES_XML):
    """Lay down es_find_rules.xml and the named component launchers."""
    files_dir = str(_user_files_dir(tmp_path))
    if find_rules is not None:
        _write(_find_rules_path(files_dir, flavor=flavor), find_rules)
    for component in installed:
        _touch(_component_launcher(files_dir, component))
    for component in external:
        _touch(_external_component_launcher(str(tmp_path), component))
    return EsFindRulesAdapter(logger=_TEST_LOGGER, user_home=str(tmp_path))


class TestEmulatorToken:
    """``emulator_token`` extracts the find-rule name from a command."""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("%EMULATOR_RYUBING% %ROM%", "RYUBING"),
            ("env QT_QPA_PLATFORM=xcb %EMULATOR_DOLPHIN% -b -e %ROM%", "DOLPHIN"),
            ("%EMULATOR_PICO-8% -root_path %GAMEDIR% -run %ROM%", "PICO-8"),
            ("%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/swanstation_libretro.so %ROM%", "RETROARCH"),
            ("no token here %ROM%", None),
        ],
    )
    def test_token_extraction(self, command, expected):
        assert emulator_token(command) == expected


class TestFindEsFindRulesXml:
    def test_finds_the_linux_flavor(self, tmp_path):
        adapter = _seed(tmp_path, flavor="linux")
        found = adapter.find_es_find_rules_xml()
        assert found is not None
        assert found.endswith(os.path.join("systems", "linux", "es_find_rules.xml"))

    def test_falls_back_to_the_unix_flavor(self, tmp_path):
        adapter = _seed(tmp_path, flavor="unix")
        found = adapter.find_es_find_rules_xml()
        assert found is not None
        assert found.endswith(os.path.join("systems", "unix", "es_find_rules.xml"))

    def test_prefers_linux_when_both_flavors_exist(self, tmp_path):
        adapter = _seed(tmp_path, flavor="linux")
        _write(_find_rules_path(str(_user_files_dir(tmp_path)), flavor="unix"), _FIND_RULES_XML)
        found = adapter.find_es_find_rules_xml()
        assert found is not None
        assert found.endswith(os.path.join("systems", "linux", "es_find_rules.xml"))

    def test_returns_none_when_absent(self, tmp_path):
        adapter = _seed(tmp_path, find_rules=None)
        assert adapter.find_es_find_rules_xml() is None


class TestDeployPriority:
    """The user installation wins over the system one, as ``flatpak run`` resolves it.

    The isolation fixture above repoints the system root away for every other
    test here, so these two put it back at a second seeded tree and pin which of
    the two the adapter reads. The stake is that the catalogue and the find rules
    describe the same RetroDECK: the vendored resolver reads the catalogue out of
    the deploy flatpak would run, and a system-first probe here would answer a
    launch decision from the other one.
    """

    def _both_deploys(self, tmp_path, *, user_rules: str, system_rules: str):
        system_root = tmp_path / "system_flatpak"
        system_files = system_root / "app" / "net.retrodeck.retrodeck" / "current" / "active" / "files"
        _write(_find_rules_path(str(system_files), flavor="linux"), system_rules)
        _write(_find_rules_path(str(_user_files_dir(tmp_path)), flavor="linux"), user_rules)
        return system_root

    def test_the_user_deploy_answers_where_both_carry_the_app(self, tmp_path):
        system_root = self._both_deploys(
            tmp_path,
            user_rules=_FIND_RULES_XML,
            system_rules=_FIND_RULES_XML.replace("components/rpcs3/", "components/system-rpcs3/"),
        )
        with mock.patch("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", str(system_root)):
            adapter = EsFindRulesAdapter(logger=_TEST_LOGGER, user_home=str(tmp_path))
            found = adapter.find_es_find_rules_xml()
            assert found is not None
            assert found.startswith(str(_user_files_dir(tmp_path)))
            assert (
                adapter.resolve_sandbox_launcher("%EMULATOR_RPCS3% --no-gui %ROM%")
                == "/app/retrodeck/components/rpcs3/component_launcher.sh"
            )

    def test_the_system_deploy_answers_when_it_is_the_only_one(self, tmp_path):
        system_root = tmp_path / "system_flatpak"
        system_files = system_root / "app" / "net.retrodeck.retrodeck" / "current" / "active" / "files"
        _write(_find_rules_path(str(system_files), flavor="linux"), _FIND_RULES_XML)
        with mock.patch("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", str(system_root)):
            adapter = EsFindRulesAdapter(logger=_TEST_LOGGER, user_home=str(tmp_path))
            found = adapter.find_es_find_rules_xml()
            assert found is not None
            assert found.startswith(str(system_files))

    def test_an_app_component_is_probed_in_the_user_deploy_first(self, tmp_path):
        # The same order has to hold for the /app prefix mapping, or the probe
        # answers "installed" off a component in the deploy that will not run.
        system_root = self._both_deploys(tmp_path, user_rules=_FIND_RULES_XML, system_rules=_FIND_RULES_XML)
        system_files = system_root / "app" / "net.retrodeck.retrodeck" / "current" / "active" / "files"
        _touch(_component_launcher(str(system_files), "ppsspp"))
        with mock.patch("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", str(system_root)):
            adapter = EsFindRulesAdapter(logger=_TEST_LOGGER, user_home=str(tmp_path))
            # Present in the system deploy only — still "installed", because the
            # probe checks every root; the ORDER is what the two tests above pin.
            assert adapter.command_emulator_installed("%EMULATOR_PPSSPP% -b %ROM%") is True


class TestCommandEmulatorInstalled:
    """The absence-only probe behind ADR-0020's standalone downgrade."""

    def test_missing_retrodeck_component_is_not_installed(self, tmp_path):
        adapter = _seed(tmp_path, installed=["ppsspp"])
        assert adapter.command_emulator_installed("%EMULATOR_RYUBING% %ROM%") is False

    def test_installed_bundled_component_is_installed(self, tmp_path):
        adapter = _seed(tmp_path, installed=["ppsspp"])
        assert adapter.command_emulator_installed("%EMULATOR_PPSSPP% -b %ROM%") is True

    def test_external_component_counts_as_installed(self, tmp_path):
        adapter = _seed(tmp_path, external=["ryubing"])
        assert adapter.command_emulator_installed("%EMULATOR_RYUBING% %ROM%") is True

    def test_systempath_only_emulator_is_assumed_installed(self, tmp_path):
        # No staticpath rule — unverifiable from outside the sandbox, so the
        # probe never acts on it.
        adapter = _seed(tmp_path)
        assert adapter.command_emulator_installed("%EMULATOR_ATARI800% %ROM%") is True

    def test_host_only_staticpaths_are_assumed_installed(self, tmp_path):
        # Nothing on disk, but no entry names a RetroDECK component — a host
        # install cannot be disproven from here.
        adapter = _seed(tmp_path)
        assert adapter.command_emulator_installed("%EMULATOR_HOSTONLY% %ROM%") is True

    def test_unknown_token_is_assumed_installed(self, tmp_path):
        adapter = _seed(tmp_path)
        assert adapter.command_emulator_installed("%EMULATOR_NOSUCHRULE% %ROM%") is True

    def test_a_command_naming_no_emulator_is_assumed_installed(self, tmp_path):
        adapter = _seed(tmp_path)
        assert adapter.command_emulator_installed("just some text %ROM%") is True

    def test_absent_find_rules_leave_everything_installed(self, tmp_path):
        adapter = _seed(tmp_path, find_rules=None)
        assert adapter.command_emulator_installed("%EMULATOR_RYUBING% %ROM%") is True

    def test_the_probe_sees_a_component_that_appears_later(self, tmp_path):
        # The parse is cached; the on-disk probe is not. A component the user
        # installs mid-session flips the verdict with no cache reset at all.
        adapter = _seed(tmp_path)
        assert adapter.command_emulator_installed("%EMULATOR_RYUBING% %ROM%") is False
        _touch(_component_launcher(str(_user_files_dir(tmp_path)), "ryubing"))
        assert adapter.command_emulator_installed("%EMULATOR_RYUBING% %ROM%") is True


class TestResolveSandboxLauncher:
    """``resolve_sandbox_launcher`` picks a standalone command's sandbox launcher.

    Returns the sandbox-absolute RetroDECK component ``staticpath`` verbatim (no
    on-disk existence check — the default / pin resolution already gated
    installedness).
    """

    def test_resolves_the_app_component_over_host_entries(self, tmp_path):
        adapter = _seed(tmp_path)
        assert (
            adapter.resolve_sandbox_launcher("%EMULATOR_RPCS3% --no-gui %ROM%")
            == "/app/retrodeck/components/rpcs3/component_launcher.sh"
        )

    def test_prefers_app_over_var_data_component(self, tmp_path):
        adapter = _seed(tmp_path)
        assert (
            adapter.resolve_sandbox_launcher("%EMULATOR_RYUBING% %ROM%")
            == "/app/retrodeck/components/ryubing/component_launcher.sh"
        )

    def test_var_data_external_component_resolves(self, tmp_path):
        adapter = _seed(tmp_path)
        assert (
            adapter.resolve_sandbox_launcher("%EMULATOR_EXTONLY% %ROM%")
            == "/var/data/retrodeck/external_components/extonly/component_launcher.sh"
        )

    def test_host_only_staticpaths_yield_none(self, tmp_path):
        adapter = _seed(tmp_path)
        assert adapter.resolve_sandbox_launcher("%EMULATOR_HOSTONLY% %ROM%") is None

    def test_pipe_suffixed_entry_is_stripped(self, tmp_path):
        adapter = _seed(tmp_path)
        assert (
            adapter.resolve_sandbox_launcher("%EMULATOR_PIPED% %ROM%")
            == "/app/retrodeck/components/piped/component_launcher.sh"
        )

    def test_systempath_only_emulator_yields_none(self, tmp_path):
        adapter = _seed(tmp_path)
        assert adapter.resolve_sandbox_launcher("%EMULATOR_ATARI800% %ROM%") is None

    def test_unknown_emulator_token_yields_none(self, tmp_path):
        adapter = _seed(tmp_path)
        assert adapter.resolve_sandbox_launcher("%EMULATOR_UNKNOWN% %ROM%") is None

    def test_command_without_emulator_token_yields_none(self, tmp_path):
        adapter = _seed(tmp_path)
        assert adapter.resolve_sandbox_launcher("just some text %ROM%") is None

    def test_missing_find_rules_yields_none(self, tmp_path):
        adapter = _seed(tmp_path, find_rules=None)
        assert adapter.resolve_sandbox_launcher("%EMULATOR_RPCS3% --no-gui %ROM%") is None


class TestStaticPathMapping:
    """Each sandbox prefix a find rule may use resolves to its host location.

    Only the ``/app`` and ``/var/data`` shapes appear in the rule fixtures above,
    because those are what RetroDECK actually ships. The rest are the mapping's
    own branches, exercised through the probe so a wrong host path shows up as a
    wrong verdict rather than as a passing unit assertion about a string.
    """

    _RULES = """\
<?xml version="1.0"?>
<ruleList>
  <emulator name="VARCONFIG">
    <rule type="staticpath">
      <entry>/var/config/retrodeck/components/varconfig/component_launcher.sh</entry>
    </rule>
  </emulator>
  <emulator name="TILDE">
    <rule type="staticpath">
      <entry>~/Applications/tilde*.AppImage</entry>
    </rule>
  </emulator>
  <emulator name="HOME">
    <rule type="staticpath">
      <entry>~</entry>
    </rule>
  </emulator>
  <emulator name="LITERAL">
    <rule type="staticpath">
      <entry>/var/lib/flatpak/exports/bin/net.example.Literal</entry>
    </rule>
  </emulator>
  <emulator name="EMPTYENTRY">
    <rule type="staticpath">
      <entry></entry>
    </rule>
  </emulator>
</ruleList>
"""

    def test_a_var_config_component_maps_under_the_app_config_tree(self, tmp_path):
        adapter = _seed(tmp_path, find_rules=self._RULES)
        assert adapter.command_emulator_installed("%EMULATOR_VARCONFIG% %ROM%") is False

        _touch(
            os.path.join(
                str(tmp_path),
                ".var",
                "app",
                "net.retrodeck.retrodeck",
                "config",
                "retrodeck",
                "components",
                "varconfig",
                "component_launcher.sh",
            )
        )
        assert adapter.command_emulator_installed("%EMULATOR_VARCONFIG% %ROM%") is True

    def test_a_tilde_entry_globs_under_the_user_home(self, tmp_path):
        adapter = _seed(tmp_path, find_rules=self._RULES)
        # No RetroDECK component among its entries, so an absent host install is
        # never a downgrade — the launcher lookup is what the mapping shows.
        assert adapter.resolve_sandbox_launcher("%EMULATOR_TILDE% %ROM%") is None
        _touch(os.path.join(str(tmp_path), "Applications", "tilde-1.2.AppImage"))
        assert adapter.command_emulator_installed("%EMULATOR_TILDE% %ROM%") is True

    def test_a_bare_tilde_is_the_user_home_itself(self, tmp_path):
        adapter = _seed(tmp_path, find_rules=self._RULES)
        assert adapter.command_emulator_installed("%EMULATOR_HOME% %ROM%") is True

    def test_any_other_absolute_path_is_taken_literally(self, tmp_path):
        adapter = _seed(tmp_path, find_rules=self._RULES)
        assert adapter.command_emulator_installed("%EMULATOR_LITERAL% %ROM%") is True

    def test_an_empty_entry_resolves_to_nothing(self, tmp_path):
        adapter = _seed(tmp_path, find_rules=self._RULES)
        assert adapter.command_emulator_installed("%EMULATOR_EMPTYENTRY% %ROM%") is True
        assert adapter.resolve_sandbox_launcher("%EMULATOR_EMPTYENTRY% %ROM%") is None


class TestParseFailures:
    """A file that cannot be read or parsed leaves every emulator assumed installed."""

    def test_invalid_xml_parses_to_nothing(self, tmp_path):
        adapter = _seed(tmp_path, find_rules="<ruleList><emulator name='X'>")
        assert adapter.command_emulator_installed("%EMULATOR_RYUBING% %ROM%") is True
        assert adapter.resolve_sandbox_launcher("%EMULATOR_RPCS3% --no-gui %ROM%") is None

    def test_an_unreadable_file_parses_to_nothing(self, tmp_path):
        adapter = _seed(tmp_path)
        path = adapter.find_es_find_rules_xml()
        assert path is not None
        assert adapter.parse_es_find_rules(os.path.join(path, "not-a-directory")) == {}


class TestMtimeInvalidation:
    def test_the_parse_is_reread_after_the_file_changes(self, tmp_path):
        adapter = _seed(tmp_path)
        assert adapter.resolve_sandbox_launcher("%EMULATOR_RPCS3% --no-gui %ROM%") is not None

        path = _find_rules_path(str(_user_files_dir(tmp_path)), flavor="linux")
        _write(path, "<?xml version='1.0'?>\n<ruleList></ruleList>\n")
        os.utime(path, (0, 0))

        assert adapter.resolve_sandbox_launcher("%EMULATOR_RPCS3% --no-gui %ROM%") is None

    def test_an_unchanged_file_answers_from_the_cache(self, tmp_path):
        adapter = _seed(tmp_path)
        first = adapter._load_find_rules()
        assert adapter._load_find_rules() is first
