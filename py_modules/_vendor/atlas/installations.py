"""Installation handles — every question is asked *of an installation*.

Detection produces these; each answers ``savefile_location`` for its own flavor by
reading the configs that govern it — the same files, in the same order, with the
same fallbacks the emulator itself uses — through the injected machine seam.

- :class:`RetroDeck` — the RetroDECK Flatpak. ``retrodeck.json`` supplies the
  roots and the health check; the bundled RetroArch's cfg (plus its override
  chain) supplies the layout. The cfg is what RetroArch reads, so the cfg is the
  truth; ``retrodeck.json`` is context.
- :class:`EmuDeck` — an EmuDeck arrangement. Its truth is ``settings.sh``; its
  RetroArch is the bare ``org.libretro.RetroArch`` Flatpak, so the handle
  carries both descriptions (``kinds``) — the same RetroArch under two names.
- :class:`BareRetroArchFlatpak` / :class:`BareRetroArchNative` — bare
  RetroArch installs, differing in config location and default set.

Health is reported, not guessed around: a readable config whose root points into
an absent mount (unmounted SD card) yields ``root_missing``, never a
syntactically correct path handed out as if it were usable.
"""

from __future__ import annotations

import json
import os
import shlex
import tomllib
import xml.etree.ElementTree as _ET
from glob import escape as _glob_escape
from typing import (
    Any,
    Callable,
    Iterator,
    Mapping,
    NamedTuple,
    Protocol,
    Sequence,
    cast,
    runtime_checkable,
)

from dataclasses import dataclass, replace as _dc_replace

from .content_path import (
    content_basename,
    content_file_name,
    content_system_dir,
    split_content_path,
)
from .content_tree_wiring import WiringRow, lookup_content_tree_wiring
from .core_info import parse_core_info
from .esde import (
    INVALID_PARSE,
    KIND_LIBRETRO,
    CatalogueLayer,
    EmulatorSpec,
    GamelistSelections,
    SystemDeclaration,
    commented_out_systems,
    emulator_token,
    esde_extension,
    expand_home_path,
    merge_layers,
    parse_es_settings,
    parse_es_systems,
    parse_gamelist,
    resolve_rom_path,
)
from .evidence import arrangement_caveats
from .platforms import (
    CAVEAT_PLATFORM_SCRAPING_IGNORED,
    CAVEAT_PLATFORM_UNKNOWN,
    CAVEAT_PLATFORM_UNMAPPED,
    PlatformIdentities,
    platform_identities,
    platforms_for,
)
from .systems import known_systems, vocabulary_platform_tags
from .firmware import (
    CAVEAT_CORE_DIR_UNRESOLVED,
    CAVEAT_CORE_ENUMERATION_INCOMPLETE,
    CAVEAT_CORE_INFO_UNREADABLE,
    CAVEAT_EMULATOR_CATALOGUE_EXCLUSIVE,
    CAVEAT_EMULATOR_CATALOGUE_SEALED,
    CAVEAT_EMULATOR_CATALOGUE_UNAVAILABLE,
    CAVEAT_EMULATOR_CATALOGUE_UNESTABLISHED,
    CAVEAT_EMULATOR_CATALOGUE_UNREADABLE,
    CAVEAT_EMULATOR_LIST_DERIVED,
    CAVEAT_FIRMWARE_ROOT_MISSING,
    CAVEAT_INFO_PATH_UNRESOLVED,
    CAVEAT_SYSTEM_UNKNOWN,
    derived_core_selection,
    derived_enumeration_lead,
    Catalogue,
    CatalogueEntry,
    CoreDeclarations,
    FirmwareAnswer,
    FirmwareContext,
    FirmwareIdentification,
    SYSTEMS_WITHOUT_CATALOGUE_ID,
    load_hashes,
    read_core_declarations,
    xemu_file_value,
)
from .launch_formats import lookup_install_first, lookup_standalone_launch
from .firmware import firmware_for_core as _resolve_for_core
from .firmware import firmware_for_system as _resolve_for_system
from .firmware import firmware_inventory as _resolve_inventory
from .firmware import identify_firmware as _resolve_identification
from .machine import (
    GLOB_COMPLETE,
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_INACCESSIBLE,
    KIND_MISSING,
    READ_MISSING,
    READ_OK,
    SYMLINK_HOPS,
    CoreInfo,
    CoreOption,
    GlobResult,
    Machine,
    ReadResult,
    ReadStatus,
)
from .mods import (
    ModCard,
    ModSetting,
    SoftPatchBuild,
    StandaloneModCard,
    lookup_mod_card,
    lookup_soft_patch_build,
    lookup_standalone_mod_card,
)
from .mode_rules import (
    FILE_ABSENT,
    FILE_READ,
    FILE_UNREADABLE,
    RULES as MODE_RULES,
    FileLookup,
    RuleReading,
)
from .oddities import (
    MODE_ALWAYS,
    CoreCard,
    RetiredOption,
    SaveMode,
    VerifiedOn,
    lookup_audit,
    lookup_card,
)
from .save_memory import SaveMemoryRecord, SystemMemory, lookup_save_memory
from .textures import (
    XDG_CONFIG,
    XDG_DATA,
    StandaloneTextureCard,
    TextureCard,
    TextureSetting,
    lookup_standalone_texture_card,
    lookup_texture_card,
)
from .placement import (
    UNRESOLVED_CORE_NOT_INSTALLED,
    CAVEAT_APP_RELATIVE_PATH_UNEXPANDED,
    CAVEAT_CFG_LINE_DROPPED,
    CAVEAT_CFG_VALUE_REJECTED,
    CAVEAT_CONTENT_DIR_OBSERVATION,
    CAVEAT_CONTENT_PATH_UNNAMED,
    CAVEAT_CORE_GENERATION_MISMATCH,
    CAVEAT_CORE_GENERATION_UNESTABLISHED,
    CAVEAT_CORE_MODE_UNESTABLISHED,
    CAVEAT_CORE_OWN_WRITES_UNESTABLISHED,
    CAVEAT_CORE_OPTION_VALUE_UNESTABLISHED,
    CAVEAT_CORE_SAVESTATES_UNSUPPORTED,
    CAVEAT_DEAD_SYMLINK,
    CAVEAT_OPTION_ENTRY_RETIRED,
    CAVEAT_FILE_NAMES_UNESTABLISHED,
    CAVEAT_FILE_SET_ACROSS_SYSTEMS,
    CAVEAT_EMULATOR_CONFIG_UNREAD,
    CAVEAT_EMULATOR_READ_UNESTABLISHED,
    CAVEAT_FEATURE_SWITCH_ABSENT,
    CAVEAT_SORTED_DIR_UNCREATABLE,
    CAVEAT_CORE_MULTI_OPTION,
    CAVEAT_CORE_SUSPECT,
    CAVEAT_CORE_UNAUDITED,
    CAVEAT_CORE_UNQUERYABLE,
    CAVEAT_INVALID_SAVE_DIRECTORY,
    CAVEAT_INVALID_SCREENSHOT_DIRECTORY,
    CAVEAT_UNVERIFIED_VERSION,
    CAVEAT_PER_GAME_ALTERNATIVE_EMULATOR,
    CAVEAT_PER_GAME_BUILD_LAYER_UNREAD,
    CAVEAT_PER_GAME_LAYER_UNREAD,
    CAVEAT_PER_GAME_OVERRIDE,
    CAVEAT_PER_GAME_OVERRIDES_PRESENT,
    CAVEAT_FILENAMES_CONTENT_CONDITIONAL,
    CAVEAT_FILENAMES_UNVERIFIED,
    CAVEAT_FILE_SET_SPANS_ROOTS,
    CAVEAT_NO_CORE,
    CAVEAT_PATCH_FORMATS_UNESTABLISHED,
    CAVEAT_SANDBOX_PATH_UNTRANSLATED,
    CAVEAT_SOFT_PATCHING_APPLIES,
    CAVEAT_SAVE_DIR_LAUNCH_DEPENDENT,
    CAVEAT_SAVE_DIR_UNLISTABLE,
    CAVEAT_SAVE_INSIDE_CONTENT,
    CAVEAT_SAVE_INSIDE_IMAGE,
    CAVEAT_SAVE_ROOT_REVOKED,
    CAVEAT_SAVE_WRITES_DISCARDED,
    CAVEAT_SAVESTATE_INSIDE_IMAGE,
    CAVEAT_SAVESTATE_SUPPORT_MACHINE_DEPENDENT,
    CAVEAT_SORTED_DIR_MISSING,
    CAVEAT_SYMLINK_LOOP,
    CAVEAT_SYSTEM_DIRECTORY_CLEARED,
    CAVEAT_UNKNOWN_OPTION_VALUE,
    HOLE_CONTENT_DIR,
    HOLE_CONTENT_DIR_NAME,
    HOLE_CWD,
    HOLE_REGION,
    HOLE_ROM_STEM,
    HOLE_SAVE_ID,
    PATCH_FORMATS,
    ROLE_BATTERY,
    ROLE_MEMORY_CARD,
    ROLE_SETTINGS,
    ROOT_CONTENT_DIRECTORY,
    ROOT_EMULATOR_DIRECTORY,
    ROOT_SAVEFILE_DIRECTORY,
    ROOT_SYSTEM_DIRECTORY,
    ROOT_WORKING_DIRECTORY,
    STATE_ROOT_CONTENT_DIRECTORY,
    STATE_ROOT_EMULATOR_DIRECTORY,
    STATE_ROOT_KINDS,
    STATE_ROOT_WORKING_DIRECTORY,
    StateRootKind,
    SUBDIR_TEMPLATE_HOLES,
    TEMPLATE_CONTENT_DIR,
    TEMPLATE_CONTENT_DIR_NAME,
    TEMPLATE_CWD,
    TEMPLATE_REGION,
    TEMPLATE_ROM_STEM,
    TEMPLATE_SAVE_ID,
    FILE_SET_DECLARED,
    FILE_SET_OBSERVED,
    FILE_SET_UNKNOWN,
    GRANULARITY_NONE,
    UNKNOWN_FILE_SET,
    UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
    UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
    UNRESOLVED_MOD_WIRING_UNESTABLISHED,
    UNRESOLVED_STANDALONE,
    UNRESOLVED_STANDALONE_VARIANT_UNESTABLISHED,
    UNRESOLVED_TEXTURE_WIRING_UNESTABLISHED,
    Caveat,
    FileGroup,
    FileSet,
    GRANULARITY_PER_GAME_DIRECTORY,
    GRANULARITY_PER_GAME_FILE,
    GRANULARITY_PER_GAME_FILES,
    GRANULARITY_SHARED_CARD,
    Granularity,
    ModeAlternative,
    ModPlacement,
    ModTree,
    OptionReading,
    RootKind,
    SCREENSHOT_ROOT_CONTENT_DIRECTORY,
    SCREENSHOT_ROOT_DIRECTORY,
    ScreenshotRootKind,
    SavefilePlacement,
    SavestateAbsence,
    SavestatePlacement,
    ScreenshotPlacement,
    SoftPatchAnswer,
    TexturePlacement,
    Unresolved,
    build_savefile_placement,
    build_savestate_placement,
    build_soft_patch_candidates,
    file_set_holes,
    needs_with_file_set,
)
from . import duckstation, emulator_settings, melonds, qt_ini
from .yaml_scalars import YamlScalars, read_scalars
from .standalone_saves import StandaloneSaveCard, lookup_standalone_save_card
from .standalone_savestates import (
    SavestateIniKey,
    SavestateLaunchIni,
    StandaloneSavestateCard,
    lookup_standalone_savestate_card,
)
from .retroarch_cfg import (
    IGNORED_LINE_DROPPED,
    chain_value,
    SAVEFILE_KEYS,
    SAVESTATE_KEYS,
    UPSTREAM_DEFAULTS,
    IgnoredSetting,
    LayoutDefaults,
    LayoutKeys,
    ParsedCfg,
    RejectedDirectory,
    RetroArchCfg,
    cfg_bool,
    chain_bool,
    chain_value,
    expand_home,
    is_app_relative,
    parse_cfg,
    parse_cfg_text,
    resolve_layout,
)

# Health issue codes — stable identifiers clients and vectors branch on.
# An installation is healthy exactly when it has no issues; every issue keeps
# marker existence, read status, parse status, companion state, and root state
# apart instead of collapsing them into one lossy string (REVIEW H10).
HEALTH_ISSUE_MARKER_MISSING = "marker-missing"
HEALTH_ISSUE_MARKER_UNREADABLE = "marker-unreadable"
HEALTH_ISSUE_MARKER_INVALID = "marker-invalid"
HEALTH_ISSUE_ROOT_MISSING = "root-missing"
HEALTH_ISSUE_SAVES_ROOT_MISSING = "saves-root-missing"
HEALTH_ISSUE_COMPANION_CONFIG_MISSING = "companion-config-missing"
HEALTH_ISSUE_CONFIG_UNREADABLE = "config-unreadable"
# A systems catalogue file ES-DE refuses its whole load on: one that does not
# parse, or one carrying no document-level <systemList>. Not a statement about
# one layer — the frontend aborts loadConfig outright (SystemData.cpp:879-882,
# :900-903 @ v3.4.1) and the caller turns that into INVALID_FILE
# (main.cpp:483-486), so it runs with no systems at all. The catalogue answers
# then state the frontend's truth (nothing), and this finding carries the file
# and the problem so the one edit that fixes it is named. Distinct from a file
# atlas cannot READ: ES-DE may read such a file fine, so what it says stays
# unknown there rather than refused (issue #100).
HEALTH_ISSUE_CATALOGUE_INVALID = "catalogue-invalid"
# A content hub tree (texture_packs or mods) that exists while the emulator-side
# path its arrangement pairs it with is no symlink into the hub — the
# upgraded-without-reset state (issue #104): dir_prep creates hub tree and link
# together on prepare/reset/move, a flatpak upgrade re-runs only version-gated
# patches, so an upgraded installation can carry the hub while the emulator
# reads a plain directory of its own, and content filed in the hub never
# reaches it. One finding per broken pair; ``data.problem`` keeps the three
# ways apart (missing / not-a-link / diverted). Checked only when the marker
# names exactly the version the wiring table was read at — any other version
# made promises atlas never read, so no row is checked there (fail closed).
HEALTH_ISSUE_CONTENT_TREE_UNWIRED = "content-tree-unwired"


def _content_tree_unwired_finding(
    row: WiringRow, *, version: str, hub_dir: str, path: str, problem: str, target: str | None
) -> Caveat:
    """The health finding for one dir_prep pair whose emulator-side link is gone."""
    if problem == "missing":
        state = f"nothing exists at {path}"
    elif problem == "diverted":
        state = f"the link at {path} settles at {target}, outside the {row.family} hub"
    else:
        state = f"{path} stands there as a plain path of its own, not a link"
    data = {"family": row.family, "hub": hub_dir, "path": path, "problem": problem}
    if target is not None:
        data["target"] = target
    return Caveat(
        HEALTH_ISSUE_CONTENT_TREE_UNWIRED,
        f"content filed under {hub_dir} never reaches an emulator: RetroDECK {version} pairs "
        f"that hub tree with {path} by symlink, and {state} — the pair is created on prepare, "
        "reset and folder moves only, never by an in-place upgrade, so the gap stays until one "
        f"of those runs ({row.source})",
        data,
    )


def _catalogue_invalid_finding(path: str, problem: str) -> Caveat:
    """The health finding for a catalogue file the frontend refuses to load on."""
    reason = (
        "does not parse as XML"
        if problem == INVALID_PARSE
        else "carries no document-level <systemList>"
    )
    return Caveat(
        HEALTH_ISSUE_CATALOGUE_INVALID,
        f"ES-DE refuses its whole systems catalogue: {path} {reason}, and a systems file it "
        "cannot load aborts the load of every layer (SystemData.cpp:879-882, :900-903; "
        "INVALID_FILE at main.cpp:483-486 — v3.4.1), so the frontend runs with no systems at "
        "all until the file is fixed",
        {"path": path, "problem": problem},
    )


@dataclass(frozen=True, slots=True)
class Health:
    """Structured installation health — *ok* is simply the absence of issues.

    Each issue is a :class:`~atlas.placement.Caveat` whose ``code`` is one of
    the ``HEALTH_ISSUE_*`` constants; ``data`` carries the affected path plus
    whatever else the finding established (the read ``status`` behind an
    unreadable marker, the ``key`` behind an invalid one). Handles never hide a
    present-but-broken installation — they report it with the issues attached.

    These issues *are* caveats, and they travel as themselves: every answer
    computed on a broken installation carries the findings directly in its own
    ``caveats``, under their own codes and ahead of what the query itself could
    not resolve. Nothing wraps them in a category code with the real condition
    nested in ``data`` — that shape hides a distinct, stable code behind a
    discriminator a client has to unpack, and the firmware route retired it for
    the same reason.

    Every answer, not only the ones a finding bears on: whether the arrangement
    is broken is true of the installation however it was asked, and a map of
    which finding affects which answer would have to be maintained and could
    rot into silence. What broke is in the ``data``; judging relevance is the
    caller's. Each route derives the findings from the reads it already made,
    never from a second :meth:`Installation.health` call, so an answer and the
    findings beside it were read from one revision of each source (REVIEW M4).
    """

    issues: tuple[Caveat, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

# The file RetroArch reads, and the Flatpak apps that ship one.
RETROARCH_CFG = "retroarch.cfg"
RETRODECK_APP_ID = "net.retrodeck.retrodeck"
RETROARCH_FLATPAK_APP_ID = "org.libretro.RetroArch"

# The default XDG config home's directory name under ``home`` — where the
# non-Flatpak markers live — and the data home's, its two-segment sibling.
_XDG_CONFIG_DIRNAME = ".config"
_XDG_DATA_SUFFIX = os.path.join(".local", "share")

# The user Flatpak installation's base, as a ``home``-relative suffix —
# ``g_get_user_data_dir()/flatpak`` (``flatpak_get_user_base_dir_location``,
# flatpak-dir.c:1918-1940 at 1.16.6). Deploys (``app/...``) and the overrides
# files alike hang off it.
_FLATPAK_USER_BASE = os.path.join(_XDG_DATA_SUFFIX, "flatpak")

# Where each installation keeps its deployed apps: the system one absolute, the
# user one a ``home``-relative suffix.
_FLATPAK_DEPLOY_SYSTEM = os.path.join("/var", "lib", "flatpak", "app")
_FLATPAK_DEPLOY_USER = os.path.join(_FLATPAK_USER_BASE, "app")


@dataclass(frozen=True, slots=True)
class _Deploy:
    """The one deploy of an app that a ``flatpak run`` of it would start.

    ``files`` is that deploy's ``/app`` tree as the host reads it. ``system``
    says which installation carries it — the second thing the resolution
    decides, because the system overrides files speak for a system deploy and
    for no other (flatpak-dir.c:3053-3059, :3071-3077 @ 1.16.6).
    """

    files: str
    system: bool


def _running_deploy(machine: Machine, home: str, app_id: str) -> _Deploy | None:
    """The deploy of *app_id* that runs on this machine — ``None`` where none does.

    Flatpak searches the installations in one order and stops at the first one
    that has the app deployed: the system list with the user installation
    inserted at its front (``flatpak_find_deploy_for_ref``,
    flatpak-dir-utils.c:300-316 @ 1.16.6 — ``g_ptr_array_insert (dirs, 0,
    flatpak_dir_get_user ())`` at :314), the loop ending on the first dir that
    loads a deploy (``flatpak_find_deploy_for_ref_in``, :278-285). ``flatpak
    run`` reaches the same order by its own route, which is the one that
    decides for an *app*: it moves the user dir to the front of the list it
    parsed — under the comment "Move the user dir to the front so it 'wins' in
    case an app is in more than one installation" — and searches that list
    (app/flatpak-builtins-run.c:240-255, the search at :285). So where both
    installations carry the app, the user one runs — and every question about
    the app that runs answers off this one resolution: which tree its ``/app``
    files come from, and whose overrides files speak for it.

    A deploy is read as present where the installation's ``<app id>``
    directory has a ``current/active`` — the symlink pair flatpak points at the
    deployed commit of the current branch.

    What this models is the two standard installations. Flatpak's system list
    is every location configured under ``/etc/flatpak/installations.d``, sorted
    by priority, plus the default ``/var/lib/flatpak`` (``get_system_locations``,
    flatpak-dir.c:1874-1900); atlas reads neither that configuration nor the
    deploys below such an installation, so an app deployed only in an extra
    system installation reads here as deployed nowhere, and one whose extra
    installation outranks the default reads out of the default's tree.
    """
    search_order = (
        (os.path.join(home, _FLATPAK_DEPLOY_USER), False),
        (_FLATPAK_DEPLOY_SYSTEM, True),
    )
    for base, system in search_order:
        deployed = os.path.join(base, app_id, "current", "active")
        if machine.path_kind(deployed) == KIND_DIRECTORY:
            return _Deploy(os.path.join(deployed, "files"), system)
    return None


# Config markers, as ``home``-relative suffixes.
RETRODECK_JSON_SUFFIX = os.path.join(
    ".var", "app", RETRODECK_APP_ID, "config", "retrodeck", "retrodeck.json"
)
RETRODECK_CFG_SUFFIX = os.path.join(
    ".var", "app", RETRODECK_APP_ID, "config", "retroarch", RETROARCH_CFG
)
EMUDECK_SETTINGS_SUFFIX = os.path.join(_XDG_CONFIG_DIRNAME, "EmuDeck", "settings.sh")
STANDALONE_FLATPAK_CFG_SUFFIX = os.path.join(
    ".var", "app", RETROARCH_FLATPAK_APP_ID, "config", "retroarch", RETROARCH_CFG
)
NATIVE_CFG_SUFFIX = os.path.join(_XDG_CONFIG_DIRNAME, "retroarch", RETROARCH_CFG)

# Every ini file in one directory — the glob two per-game layer checks walk
# (PCSX2's gamesettings directory, MAME's standard-ini search path).
_ANY_INI_GLOB = "*.ini"


# Flatpak binds the app's private XDG directories into the sandbox under /var, so
# an emulator running inside one writes those spellings into its own config: the
# live RetroDECK 0.10.9b cfg names its override directory
# "/var/config/retroarch/config". Verified on this machine — /var/config and
# ~/.var/app/<app id>/config are the same inode, as are /var/data and /var/cache
# against .../data and .../cache (docs/research/retrodeck-save-placement.md,
# "Flatpak sandbox spellings").
_SANDBOX_XDG_BINDS: tuple[tuple[str, str], ...] = (
    ("/var/config", "config"),
    ("/var/data", "data"),
    ("/var/cache", "cache"),
)

# Prefixes that name something else inside the sandbox than on the host, once the
# binds above, /app, and the machine's own home are ruled out: the rest of /var is
# the runtime's own filesystem (a different device from the host's /var), and
# /run/user is the sandbox's private runtime directory. Everything else a cfg can
# name — /run/media, /home, /mnt — is shared with the host and passes untouched.
_SANDBOX_ONLY_PREFIXES: tuple[str, ...] = ("/var/", "/run/user/")

# On an ostree host (Fedora Silverblue, Bazzite — both ship RetroDECK) /home is a
# symlink to /var/home, so real home directories live *under* /var and are shared
# with the sandbox like any other home. The machine's own home is checked first,
# but the cfg may spell a home path either way (RetroArch resolves the symlink,
# the caller may not), so the literal prefix is host-side too.
_OSTREE_HOME = "/var/home/"


def _app_relative_caveat(key: str, configured: str) -> Caveat:
    """A path value RetroArch resolves against its own executable's directory.

    ``fill_pathname_expand_special`` expands a leading ``:`` against
    ``fill_pathname_application_dir`` (file_path.c:1066-1101), which on Linux
    is the directory of the running RetroArch binary, read from
    ``/proc/<pid>/exe`` (file_path.c:1421-1455). That is a property of the
    process that will run, not of anything on this disk, so atlas states the
    value as configured instead of inventing an expansion for it.
    """
    return Caveat(
        CAVEAT_APP_RELATIVE_PATH_UNEXPANDED,
        f'{key} = "{configured}" is relative to the RetroArch application directory (the ":" '
        "prefix, file_path.c:1066-1101) — that directory is the running executable's own, which "
        "atlas cannot read off this machine, so the value stays unexpanded and unverified",
        {"key": key, "path": configured},
    )


@dataclass(frozen=True, slots=True)
class _CfgPath:
    """A cfg path value as the host can read it, and what the translation did.

    ``path`` is ``None`` when the value names no location atlas can read: one
    that exists only inside the Flatpak sandbox, or one RetroArch resolves
    against its own application directory (``app_relative``). Either way atlas
    says so rather than resolving against a host path that means something else.
    """

    key: str
    configured: str
    path: str | None
    translated: bool = False
    app_relative: bool = False

    @property
    def caveats(self) -> tuple[Caveat, ...]:
        """The degradation, when the configured spelling has no host location."""
        if self.path is not None:
            return ()
        if self.app_relative:
            return (_app_relative_caveat(self.key, self.configured),)
        return (
            Caveat(
                CAVEAT_SANDBOX_PATH_UNTRANSLATED,
                f'{self.key} = "{self.configured}" names a location inside the Flatpak sandbox that '
                "has no equivalent on this host — atlas cannot read what the emulator reads there",
                {"key": self.key, "path": self.configured},
            ),
        )

    @property
    def note(self) -> str:
        """Provenance suffix naming the host spelling — empty when nothing moved."""
        if self.path is None or not self.translated:
            return ""
        return f" — Flatpak sandbox path, on the host {self.path}"


@dataclass(frozen=True, slots=True)
class _Sandbox:
    """Where a Flatpak app's own cfg spellings live when read from the host.

    Every cfg value that becomes a host read passes through here: an app writes
    its config from inside its sandbox, so the paths in it are sandbox paths.
    ``app_id`` is ``None`` for a native install — its cfg is host-native, a
    ``/var/...`` value there is a real (if unusual) host path, and translating
    it would invent a location the emulator never uses.
    """

    machine: Machine
    home: str
    app_id: str | None
    # What the app's own processes substitute for a ``~`` in a cfg value: the
    # HOME their sandbox environment carries. The machine home unless a
    # Flatpak override redefines HOME for the app — the host environment's
    # HOME passes into the sandbox (flatpak-run.c:3055, flatpak 1.16.6) and an
    # [Environment] override lands on top of it (:3352), with nothing
    # reapplied after — and ``None`` where that environment carries no usable
    # HOME at all (unset, or the empty string): RetroArch then leaves a ``~``
    # exactly as written, because the substitution block is skipped when the
    # home comes back empty (fill_pathname_expand_special →
    # fill_pathname_home_dir → getenv("HOME"); file_path.c:1066-1101,
    # :1457-1468 @ a79435a).
    expansion_home: str | None

    def host(self, key: str, path: str) -> _CfgPath:
        """*path* as this host reads it, with the provenance *key* names it by."""
        translated, was_sandbox = self._translate(path)
        return _CfgPath(key, path, translated, was_sandbox)

    def bundled(self, path: str) -> str | None:
        """A path the app ships inside its own tree, as the host reads it.

        No config key is involved, so no provenance travels with it: the caller
        knows which file it asked for and reports a miss in its own terms.
        """
        return self.translate(path)

    def translate(self, path: str) -> str | None:
        """*path* as this host reads it, or ``None`` where no host path exists.

        The bare translation under :meth:`host` and :meth:`bundled`, for a
        caller that says where the path came from in its own words — the
        firmware route names the configuration key in its own caveat, and
        satisfies :class:`atlas.firmware.SandboxTranslation` by having this.
        """
        return self._translate(path)[0]

    def _translate(self, path: str) -> tuple[str | None, bool]:
        """``(host path or None, whether this was a sandbox spelling)``.

        The XDG binds are a deterministic per-app mapping, so they translate
        unconditionally and the caller's own existence check stays the one that
        decides usability. ``/app`` is the tree of the deploy that runs, which
        is a resolution of its own (:func:`_running_deploy`).
        """
        app_id = self.app_id
        if app_id is None or not path.startswith("/") or self._is_host_home(path):
            return path, False
        for prefix, xdg_dir in _SANDBOX_XDG_BINDS:
            if path == prefix or path.startswith(prefix + "/"):
                rest = path[len(prefix) :].lstrip("/")
                app_dir = os.path.join(self.home, ".var", "app", app_id, xdg_dir)
                return (os.path.join(app_dir, rest) if rest else app_dir), True
        if path.startswith("/app/"):
            return self._deployment_path(app_id, path[len("/app/") :]), True
        if path.startswith(_SANDBOX_ONLY_PREFIXES):
            return None, True
        return path, False

    def _is_host_home(self, path: str) -> bool:
        """Does *path* lie in a home directory, which is shared with the sandbox?

        Checked before the sandbox-only prefixes because an ostree host puts
        real homes under ``/var/home`` — a machine whose home is
        ``/var/home/deck`` must not have its own configured directories read as
        sandbox-internal.
        """
        return path == self.home or path.startswith((self.home + "/", _OSTREE_HOME))

    def _deployment_path(self, app_id: str, rest: str) -> str | None:
        """The app's ``/app`` tree on the host: the running deploy's ``files/``.

        One installation answers — the one whose deploy runs
        (:func:`_running_deploy`) — so a machine carrying the app in both
        reads the files the emulator really opens, not whichever copy exists.
        A path the running deploy does not carry is no host path at all: what
        is missing inside the sandbox is missing, and another installation's
        copy is not what the app would open instead.
        """
        deploy = _running_deploy(self.machine, self.home, app_id)
        if deploy is None:
            return None
        candidate = os.path.join(deploy.files, rest)
        return candidate if self.machine.path_kind(candidate) != KIND_MISSING else None

    def cfg_path(self, key: str, raw: str | None) -> _CfgPath | None:
        """One cfg key resolved to a host path — ``None`` when the key is unset.

        Blank and the literal ``"default"`` are RetroArch's "unset" spellings
        (:func:`expand_home`), and unset is the caller's own question. An
        application-relative value is set but unreadable from here, which is a
        third answer: the value comes back with no host path and the caveat
        that says why.
        """
        if raw is None:
            return None
        if is_app_relative(raw):
            return _CfgPath(key, raw, None, app_relative=True)
        expanded = expand_home(raw, home=self.expansion_home)
        return self.host(key, expanded) if expanded is not None else None


def _core_directory_in(sandbox: _Sandbox, global_text: str) -> str | None:
    """A cfg snapshot's ``libretro_directory`` as a host path, or ``None``.

    Where the core binaries live is a question every arrangement asks the same
    way; only which app's spellings the cfg is written in differs.
    """
    resolved = sandbox.cfg_path("libretro_directory", parse_cfg_text(global_text).get("libretro_directory"))
    return resolved.path if resolved is not None else None


@dataclass(frozen=True, slots=True)
class _CoreLookup:
    """Where a named core would be on this machine, and what it took to say so.

    ``so_path`` is the path to stat and load; ``cores_dir`` is the directory it
    was composed against. The directory rides along because *absence* is a
    claim: a missing ``.so`` under a directory atlas really read establishes
    that the core is not installed, while the same missing ``.so`` under a
    directory that could not be read establishes nothing at all. Both are
    ``None`` when the cfg names no resolvable ``libretro_directory``; a caller
    who passed a full path gets that path with no directory, because the stat on
    the path itself is then the whole evidence.
    """

    so_path: str | None = None
    cores_dir: str | None = None


def _core_path_from(sandbox: _Sandbox, global_text: str | None, core_so: str) -> _CoreLookup:
    """Resolve a core ``.so`` basename against a cfg snapshot's ``libretro_directory``.

    The configured value is written in the app's own spelling (live:
    ``/app/retrodeck/components/...``) and is translated to where the host
    reads it. An empty lookup when nothing resolvable — never a guess. One
    resolver for every arrangement, taking the query's own sandbox so the cfg is
    read through the same spellings everywhere in that query.
    """
    if global_text is None:
        return _CoreLookup()
    cores_dir = _core_directory_in(sandbox, global_text)
    if cores_dir is None:
        return _CoreLookup()
    return _CoreLookup(os.path.join(cores_dir, core_so), cores_dir)


# One file of the override chain as it is read: its provenance label and its
# text, in load order (global cfg first, game override last).
_CfgLayer = tuple[str, str]


def _resolve_symlink_chain(machine: Machine, path: str) -> tuple[str | None, list[tuple[str, str]]]:
    """Resolve symlink components in *path* through the seam, kernel-style.

    Walks components left to right via ``readlink``, splicing targets in
    (relative targets against the link's directory). Returns the fully resolved
    path and every ``(link, target)`` traversed — an empty list means no symlink
    was involved.

    ``None`` when the chain does not settle within
    :data:`~atlas.machine.SYMLINK_HOPS` hops. The kernel refuses the whole
    resolution there (``ELOOP``) rather than stopping partway, and so does this:
    the path the walk had reached after the last hop is still a link, and
    handing it back named a directory nothing can ever open as if it were the
    physical one — silently, because it looks like an ordinary answer. The links
    traversed are returned either way, so the caller can name the chain.

    One of three kernel walks in atlas, next to
    :func:`atlas.firmware.resolve_links` (which answers the path alone) and
    ``FixtureMachine._resolve`` (which additionally refuses to step through a
    non-directory, because it answers for paths rather than resolving them).
    This one exists for the links: a caveat has to name the chain it refused.
    A fidelity finding about symlinks, ``..`` or the hop limit belongs in all
    three; the limit itself is shared, never copied.
    """
    links: list[tuple[str, str]] = []
    current = path
    while True:
        step = _splice_first_link(machine, current)
        if step is None:
            return current, links
        if len(links) == SYMLINK_HOPS:
            return None, links
        current, link = step
        links.append(link)


def _splice_first_link(machine: Machine, path: str) -> tuple[str, tuple[str, str]] | None:
    """One kernel-style step: the leftmost symlink component replaced by its target.

    Returns the spliced path and the ``(link, target)`` traversed, or ``None``
    when no component is a link — the path is then fully resolved. A relative
    target is spliced against the directory holding the link.
    """
    parts = path.split("/")
    for i in range(2, len(parts) + 1):
        prefix = "/".join(parts[:i])
        target = machine.readlink(prefix)
        if target is None:
            continue
        link = (prefix, target)
        if not target.startswith("/"):
            target = os.path.normpath(os.path.join(os.path.dirname(prefix), target))
        rest = "/".join(parts[i:])
        return target + ("/" + rest if rest else ""), link
    return None


def _link_view(machine: Machine, directory: str) -> tuple[str | None, tuple[Caveat, ...]]:
    """The link-resolved view of a final directory (REVIEW M7).

    Returns ``(physical_dir, caveats)``: ``physical_dir`` is the fully
    resolved backing directory when *directory* traverses live symlinks —
    RetroDECK's ``dir_prep`` pattern makes the emulator-side path and the
    physical path two truthful answers to different questions. A traversal
    that ends nowhere yields a ``dead-symlink`` caveat instead: the
    emulator-side directory is dead, and writing there will fail.

    A chain that never settles is the other way to end nowhere, and it is its
    own code: a dead link points at a name that could be created, while a loop
    is a cycle somebody has to break, and the two are not one fact.
    """
    resolved, links = _resolve_symlink_chain(machine, directory)
    if not links:
        return None, ()
    if resolved is None:
        link, target = links[0]
        return None, (
            Caveat(
                CAVEAT_SYMLINK_LOOP,
                f"{directory} enters the symlink {link} -> {target} and never settles — the chain is "
                f"longer than the {SYMLINK_HOPS} hops the kernel follows, so it answers ELOOP and "
                "nothing can be read or written through this directory",
                {"path": directory, "link": link, "target": target},
            ),
        )
    if machine.path_kind(resolved) == KIND_MISSING:
        link, target = links[-1]
        return None, (
            Caveat(
                CAVEAT_DEAD_SYMLINK,
                f"{directory} traverses the symlink {link} -> {target}, which resolves to "
                f"{resolved} — a path that does not exist: the emulator-side directory is dead",
                {"link": link, "target": target, "resolved": resolved},
            ),
        )
    return (resolved if resolved != directory else None), ()


def _ignored_caveats(ignored: Sequence[IgnoredSetting]) -> tuple[Caveat, ...]:
    """State the settings the configs make and RetroArch does not apply."""
    return tuple(map(_ignored_caveat, ignored))


def _ignored_caveat(setting: IgnoredSetting) -> Caveat:
    """One ignored setting as a caveat — the file says it, the emulator does not do it."""
    if setting.kind == IGNORED_LINE_DROPPED:
        return Caveat(
            CAVEAT_CFG_LINE_DROPPED,
            f"{setting.layer}: the line {setting.text!r} sets nothing — a key must be followed by "
            "'=' after optional whitespace, and '=' is itself a key character, so RetroArch's "
            f"parser drops the line (config_file.c:596-623) and {setting.key} stays unset by it",
            {"key": setting.key, "line": setting.text},
        )
    return Caveat(
        CAVEAT_CFG_VALUE_REJECTED,
        f'{setting.layer}: {setting.key} = "{setting.text}" is not a value RetroArch accepts — a '
        "boolean is exactly 1, true, 0 or false, case-sensitively (config_file.c:1227-1262) — so "
        "the setting keeps the value it had before this file; it does not become false",
        {"key": setting.key, "value": setting.text},
    )


def _global_options_file(
    layers: Sequence[_CfgLayer], *, sandbox: _Sandbox, retroarch_config_dir: str
) -> tuple[str, tuple[Caveat, ...]]:
    """The global core-options file: ``core_options_path``, else the default name.

    Read through the chain like RetroArch does, and translated out of its
    sandbox spelling; an untranslatable one keeps the configured value, so the
    read misses and the caveat says why instead of the answer claiming the
    default file governed.
    """
    raw, ignored = chain_value(layers, "core_options_path")
    caveats = _ignored_caveats(ignored)
    configured = sandbox.cfg_path("core_options_path", raw)
    if configured is None:
        return os.path.join(retroarch_config_dir, "retroarch-core-options.cfg"), caveats
    return (
        configured.path if configured.path is not None else configured.configured,
        (*caveats, *configured.caveats),
    )


def _option_file_candidates(
    *,
    override_config_dir: str,
    global_file: str,
    library_name: str | None,
    content_dir_name: str | None,
    rom_stem: str | None,
    game_specific_options: bool,
    per_core_options: bool,
) -> list[str]:
    """The options files that could govern an option, in RetroArch's priority order.

    Game ``.opt``, folder ``.opt``, per-core ``.opt`` (when
    ``global_core_options`` is off), then the global options file — the same
    order ``validate_per_core_options`` walks.

    Every path but the global file is keyed by ``library_name``, so an unknown
    one leaves only the global file to read. That is not a degradation this
    function has to state any more: ``library_name`` is unknown exactly when the
    core could not be queried, and :func:`_select_card` does not let a card
    reach this code path at all in that case.
    """
    candidates: list[str] = []
    if library_name and game_specific_options:
        if rom_stem:
            candidates.append(os.path.join(override_config_dir, library_name, f"{rom_stem}.opt"))
        if content_dir_name:
            candidates.append(os.path.join(override_config_dir, library_name, f"{content_dir_name}.opt"))
    if library_name and per_core_options:
        candidates.append(os.path.join(override_config_dir, library_name, f"{library_name}.opt"))
    candidates.append(global_file)
    return candidates


def _core_options_value(
    machine: Machine,
    *,
    override_config_dir: str,
    global_file: str,
    library_name: str | None,
    content_dir_name: str | None,
    rom_stem: str | None,
    option_key: str,
    option_default: str | None,
    game_specific_options: bool,
    per_core_options: bool,
    retired: tuple[RetiredOption, ...] = (),
) -> tuple[str | None, str, str, tuple[tuple[RetiredOption, str], ...]]:
    """Read a core option the way RetroArch does — first existing file is THE source.

    Priority (``runloop.c`` ``validate_per_core_options``): game ``.opt``,
    folder ``.opt``, per-core ``.opt`` (when ``global_core_options`` is off),
    then *global_file*. A key absent from the governing file falls back to the
    core default — it does not fall through to another file.

    Returns ``(value, provenance, options_file, retired_found)``, where
    ``options_file`` is the file a caller would edit to change the option. The
    value is ``None`` when the governing file states none and *option_default*
    is ``None`` too: the core itself did not state a default and none is
    recorded, so what governs here was never established. Substituting the
    empty string would put a value nobody read into the answer's own
    provenance.

    ``retired_found`` are the entries of *retired* the governing file carries,
    with the value each states — read off the same parse the value lookup
    already made, so stating them costs no second read of anything. Only the
    governing file is checked: a stale entry in a file RetroArch would not
    read for this core is dead twice over, and naming it would tell a caller
    to prune a file that decides nothing here.
    """
    candidates = _option_file_candidates(
        override_config_dir=override_config_dir,
        global_file=global_file,
        library_name=library_name,
        content_dir_name=content_dir_name,
        rom_stem=rom_stem,
        game_specific_options=game_specific_options,
        per_core_options=per_core_options,
    )

    for path in candidates:
        text = machine.read_text(path).text
        if text is None:
            continue
        parsed = parse_cfg_text(text)
        retired_found = tuple(
            (option, parsed[option.key]) for option in retired if option.key in parsed
        )
        if option_key in parsed:
            return (
                parsed[option_key],
                f'{os.path.basename(path)}: {option_key} = "{parsed[option_key]}"',
                path,
                retired_found,
            )
        if option_default is None:
            return (
                None,
                f"{os.path.basename(path)} has no entry for {option_key} and no default for it was "
                "established — the installed core states none and none is recorded",
                path,
                retired_found,
            )
        return (
            option_default,
            f'core default: {option_key} = "{option_default}" ({os.path.basename(path)} has no entry)',
            path,
            retired_found,
        )
    if option_default is None:
        return (
            None,
            f"no options file states {option_key} and no default for it was established — the "
            "installed core states none and none is recorded",
            global_file,
            (),
        )
    return (
        option_default,
        f'core default: {option_key} = "{option_default}" (no options file present)',
        global_file,
        (),
    )


@dataclass(frozen=True, slots=True)
class _SaveQuery:
    """One save-location question: the arrangement, its configs, and what is asked.

    The arrangements differ only in which cfg governs them and how their cores
    are found, so they hand the shared resolver the same question object —
    ``global_text`` is the global cfg's content, read exactly once by the
    caller (REVIEW M4), and ``sandbox`` says which app's spellings the cfg is
    written in (the machine home travels with it).
    """

    sandbox: _Sandbox
    global_cfg_path: str
    global_text: str | None
    cfg_label: str
    override_config_dir: str
    defaults: LayoutDefaults
    content_path: str | None
    core_so: str | None
    core_path_resolver: Callable[[str], _CoreLookup]
    arrangement: str
    arrangement_version: str | None
    # Which system the question is about, where the asker knew — the catalogue
    # routes do, because an entry is declared *for* a system. It narrows one
    # thing only: a save-memory record is keyed by core and system together, so
    # a core that behaves differently per system (mGBA answers a Game Boy
    # cartridge's clock and a GBA cartridge's not at all) can be stated
    # precisely instead of not at all. ``None`` is the honest state of the
    # direct route, which is handed a core and nothing else, and it narrows
    # nothing rather than picking one of a record's systems.
    system: str | None = None
    extra_sources: tuple[str, ...] = ()
    extra_caveats: tuple[Caveat, ...] = ()
    # The flatpak filesystem-revocation check for one resolved host path
    # (issue #103): None on the arrangements nothing sandboxes, a callable
    # answering None-or-caveat on the flatpak ones. It rides the query so the
    # save resolvers apply it to their one final directory — every route
    # through them, the entry routes included, inherits it without a second
    # wiring.
    revocation: "Callable[[str], Caveat | None] | None" = None


@dataclass(frozen=True, slots=True)
class _Content:
    """The ROM's coordinates — every value a placement fills in from the content.

    All of them stay ``None`` when no content was named: the holes then remain
    holes (``needs``), and nothing about the ROM is guessed. ``rom_stem`` is
    ``None`` for a named content path too when RetroArch's path math derives no
    name from it — the directory still resolves, but nothing is named after a
    name that does not exist.

    ``dir_path`` and ``system_dir`` are the content's directory computed by two
    different pieces of upstream path math, and they are not always the same
    string: the save side reads it off ``runtime_content_path_basename``, the
    system side off the raw content path (:func:`content_system_dir`). Each
    route answers with the one its own caller in RetroArch computes.
    """

    dir_path: str | None = None
    dir_name: str | None = None
    rom_stem: str | None = None
    system_dir: str | None = None
    # The content's extension, lowered and without the dot — the coordinate a
    # selection rule classifies content by (hatari: floppy image or hard-disk
    # image). ``None`` where no content was named or the name carries no dot.
    extension: str | None = None


def _content_coordinates(content_path: str | None) -> _Content:
    """Split a content path into the coordinates the placement asks it for.

    An empty string names nothing at all — not even a directory — so it fills
    no coordinate and every hole stays a hole, exactly as if no content had been
    passed. The caller still states *that* it was asked with one
    (:func:`_unnamed_content_caveat`), and the empty answer stays out of the
    directory: a placement's ``dir`` may not be empty.
    """
    if not content_path:
        return _Content()
    dir_path, dir_name, rom_stem = split_content_path(content_path)
    extension = os.path.splitext(content_path)[1].removeprefix(".").lower() or None
    return _Content(
        dir_path, dir_name, rom_stem or None, content_system_dir(content_path), extension
    )


def _unnamed_content_caveat(content_path: str) -> Caveat:
    """A content path RetroArch's own path math derives no name from.

    Reached by a path whose last component is empty and carries no dot
    (``/roms/psx/Game/``) — the truncation at runloop.c:8710-8711 finds nothing
    to cut, ``fill_pathname`` then concatenates the extension onto an empty
    name (file_path.c:345-358) and the save is called ``.srm`` — and by the
    empty string, which names nothing whatsoever. Naming files after an empty
    stem would make every dotfile in the directory a save, so atlas states the
    fact and observes nothing; a domain answer with a caveat, never an
    exception.
    """
    return Caveat(
        CAVEAT_CONTENT_PATH_UNNAMED,
        f"content path {content_path!r} names no file — RetroArch's path math "
        "(runloop.c:8673-8713) derives an empty name from it, so save file names are not stated "
        "and no files are observed; name the content file itself, without a trailing slash",
        {"content_path": content_path},
    )


@dataclass(frozen=True, slots=True)
class _CoreIdentity:
    """What the core binary answered about itself — and how the asking went.

    ``library_name`` is the value that names sort-by-core directories *and* the
    override directory, and it lives only in the binary; ``None`` means the
    core could not be queried (or none was named), never a guessed name.

    ``not_installed`` is set when the machine established that the named core is
    not here at all. It is the one answer no placement can be built on: every
    other state still has a directory to name, and this one has a caller asking
    about a core their machine does not have. The route hands it back as the
    typed refusal rather than resolving a location for it.
    """

    info: CoreInfo | None = None
    library_name: str | None = None
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()
    not_installed: Unresolved | None = None


# What naming no core costs, per family. The code is one — a client branches on
# CAVEAT_NO_CORE either way — but the consequences genuinely differ, and a save
# route's sentence on a savestate answer would name a mechanism (rule cards)
# that cannot reach savestates at all.
NO_CORE_FOR_SAVES = (
    "no core given — per-core overrides and recorded per-core save behaviour not checked: this "
    "answer assumes a standard core, and a core recorded as deviating (e.g. one rooted in "
    "system_directory, like Flycast) keeps its saves elsewhere entirely"
)
NO_CORE_FOR_SCREENSHOTS = (
    "no core given — per-core and per-game overrides not checked: an override can move the "
    "screenshot keys, so this answer reflects the global configuration alone"
)
NO_CORE_FOR_STATES = (
    "no core given — per-core overrides not checked, sorting by core cannot be resolved, and "
    "whether this core declares savestate support at all was not read. No recorded per-core save "
    "behaviour can move a savestate, so unlike the save answer this one is not assuming a "
    "standard core"
)


def _core_is_absent(machine: Machine, lookup: _CoreLookup) -> bool:
    """Did the machine *establish* that this core is not installed?

    Absence is a claim, and it needs a read that could have found the core. A
    ``.so`` that stats missing under a directory that stats as a directory is
    that read: the place the core would live was reached, and the core is not in
    it. Everything else refuses the claim — a cores directory that is missing,
    inaccessible or not a directory at all leaves atlas unable to look, and
    "cannot look" is not "is not there". A caller who passed a full path is
    taken at their word: the stat on that path is the whole evidence, and there
    is no directory of atlas's choosing to vouch for.
    """
    if lookup.so_path is None:
        return False
    if lookup.cores_dir is not None and machine.path_kind(lookup.cores_dir) != KIND_DIRECTORY:
        return False
    return machine.path_kind(lookup.so_path) == KIND_MISSING


def _identify_core(
    machine: Machine,
    *,
    core_so: str | None,
    core_path_resolver: Callable[[str], _CoreLookup],
    no_core_message: str = NO_CORE_FOR_SAVES,
) -> _CoreIdentity:
    """Load the named core and ask it its ``library_name`` — the same read RetroArch does."""
    if core_so is None:
        return _CoreIdentity(caveats=(Caveat(CAVEAT_NO_CORE, no_core_message),))
    lookup = _CoreLookup(core_so) if os.sep in core_so else core_path_resolver(core_so)
    if _core_is_absent(machine, lookup):
        return _CoreIdentity(
            not_installed=Unresolved(
                UNRESOLVED_CORE_NOT_INSTALLED,
                f"core {core_so!r} is not installed here — atlas read the directory RetroArch "
                "loads cores from and this one is not in it, so there is no location to answer "
                "with: a directory named for a core that cannot run would be invented, not read",
                {"core_so": core_so},
            )
        )
    so_path = lookup.so_path
    info = machine.query_core(so_path) if so_path else None
    if info is None:
        return _CoreIdentity(
            caveats=(
                Caveat(
                    CAVEAT_CORE_UNQUERYABLE,
                    f"core {core_so!r} could not be queried — library_name unknown, per-core overrides not checked",
                    {"core_so": core_so},
                ),
            )
        )
    return _CoreIdentity(
        info=info,
        library_name=info.library_name,
        sources=(
            f'core: {os.path.basename(so_path or core_so)} reports library_name "{info.library_name}"'
            " (retro_get_system_info)",
        ),
    )


@dataclass(frozen=True, slots=True)
class _OverrideGates:
    """The global-cfg switches that decide whether overrides apply, and from where."""

    auto_overrides: bool
    override_config_dir: str
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


def _override_gates(
    global_text: str | None,
    *,
    sandbox: _Sandbox,
    cfg_label: str,
    override_config_dir: str,
    config_file_dir: str,
) -> _OverrideGates:
    """The gates, read from the global cfg — an override cannot enable itself.

    ``auto_overrides_enable`` defaults true (config.def.h) and takes only
    RetroArch's boolean vocabulary; anything else leaves the default in place
    and is stated. It is read from the global cfg alone because RetroArch
    copies it into a local *before* it merges the overrides
    (``runloop.c:4941``, used at ``:5002-5003``) — an override genuinely cannot
    switch itself on or off. ``game_specific_options`` reads the same way but
    is decided later, so it is *not* a gate here: see :func:`_apply_card`.
    Where the override files are read from is :func:`_override_directory`'s
    question.
    """
    layers: list[_CfgLayer] = [(cfg_label, global_text)] if global_text is not None else []
    auto_overrides, auto_ignored = chain_bool(layers, "auto_overrides_enable", default=True)
    directory, dir_sources, dir_caveats = _override_directory(
        layers,
        sandbox=sandbox,
        cfg_label=cfg_label,
        override_config_dir=override_config_dir,
        config_file_dir=config_file_dir,
    )
    return _OverrideGates(
        auto_overrides, directory, dir_sources, (*_ignored_caveats(auto_ignored), *dir_caveats)
    )


def _override_directory(
    layers: Sequence[_CfgLayer],
    *,
    sandbox: _Sandbox,
    cfg_label: str,
    override_config_dir: str,
    config_file_dir: str,
) -> tuple[str, tuple[str, ...], tuple[Caveat, ...]]:
    """Where RetroArch looks for override ``.cfg`` and option ``.opt`` files.

    ``fill_pathname_application_special`` takes ``rgui_config_directory`` when
    it holds a value and otherwise falls back to the directory of the loaded
    ``retroarch.cfg`` (file_path_special.c:203-206) — one level above the
    platform default ``config`` subdirectory. So an *absent* key means the
    platform default, while a key set to blank or the literal ``default``
    clears the setting and moves the whole override tree up into the config
    directory itself: this key is a *handled* path setting
    (``SETTING_PATH(..., handle_setting=true)``, configuration.c:1736), so the
    generic loop writes whatever the config holds without testing it
    (:6536-6537) — an empty value included — and ``default`` is cleared
    separately at :6825-6826.

    That is the exact opposite of what blank does to ``savefile_directory``,
    which passes ``handle_setting=false`` (:1709), is skipped by that loop
    (:6534-6535), and reaches only the block that demands ``path_is_directory``
    (:6914-6933) — so a blank value there is refused and changes nothing.
    Neither is a bug; :func:`atlas.retroarch_cfg._resolve_savefile_directory`
    carries the full pair.

    A Flatpak's cfg spells the directory sandbox-side, so it is translated to
    where the host reads it; an untranslatable spelling keeps the configured
    value, so the reads miss, which is the truth (atlas cannot see those
    files) — falling back to the default directory would apply overrides that
    do not govern.

    A line RetroArch's parser drops here moves the entire override tree back to
    the platform default while the file appears to say otherwise, so it is
    stated even though the key is not a save-layout key itself.
    """
    raw, ignored = chain_value(layers, "rgui_config_directory")
    caveats = _ignored_caveats(ignored)
    if raw is None:
        return override_config_dir, (), caveats
    configured = sandbox.cfg_path("rgui_config_directory", raw)
    if configured is None:
        return (
            config_file_dir,
            (
                f'{cfg_label}: rgui_config_directory = "{raw}" — the setting is cleared, so '
                f"overrides live beside retroarch.cfg in {config_file_dir} "
                "(configuration.c:6825, file_path_special.c:196-207)",
            ),
            caveats,
        )
    return (
        configured.path if configured.path is not None else configured.configured,
        (f'{cfg_label}: rgui_config_directory = "{raw}"{configured.note}',),
        (*caveats, *configured.caveats),
    )


def _override_layers(
    machine: Machine,
    *,
    gates: _OverrideGates,
    library_name: str | None,
    content: _Content,
) -> tuple[list[tuple[str, str]], tuple[str, ...]]:
    """The override files that exist, in RetroArch's load order (configuration.c:7095).

    Core, then content-dir, then game — each read through the seam, each kept
    only if it is there. Returns the layers and any provenance the reading
    itself produced.
    """
    overrides: list[tuple[str, str]] = []
    if not gates.auto_overrides:
        return overrides, ('retroarch.cfg: auto_overrides_enable = "false" — override files not applied',)
    if library_name is None:
        return overrides, ()
    candidates = [
        (
            f"core override config/{library_name}/{library_name}.cfg",
            os.path.join(gates.override_config_dir, library_name, f"{library_name}.cfg"),
        )
    ]
    if content.dir_name:
        candidates.append(
            (
                f"content-dir override config/{library_name}/{content.dir_name}.cfg",
                os.path.join(gates.override_config_dir, library_name, f"{content.dir_name}.cfg"),
            )
        )
    if content.rom_stem:
        candidates.append(
            (
                f"game override config/{library_name}/{content.rom_stem}.cfg",
                os.path.join(gates.override_config_dir, library_name, f"{content.rom_stem}.cfg"),
            )
        )
    for label, path in candidates:
        text = machine.read_text(path).text
        if text is not None:
            overrides.append((label, text))
    return overrides, ()


@dataclass(frozen=True, slots=True)
class _SaveRoot:
    """The configured root as the host sees it — and whether it can look.

    ``reachable`` is false for a sandbox path with no host location: RetroArch's
    own "is this an existing directory" test cannot be reproduced from here,
    so it is not performed rather than answered from a read that never applied.
    """

    layout: RetroArchCfg
    reachable: bool = True
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


def _host_save_dir(sandbox: _Sandbox, layout: RetroArchCfg) -> _SaveRoot:
    """The resolved root as the host reads it — the cfg may spell it sandbox-side.

    An untranslatable spelling stays as configured: it is where the emulator
    writes, in the only namespace that names it, and the caveat states that
    atlas cannot follow it there. Substituting a host directory would answer
    with a location this RetroArch never touches. An application-relative value
    is the same kind of answer — the value as configured, unreachable from here
    — except that not even the emulator-side spelling is a path yet.

    Which key is being translated comes off the layout itself, so the savefile
    and savestate roots take the same route and each names its own key.
    """
    key = layout.keys.directory
    configured = layout.directory
    if configured is None:
        return _SaveRoot(layout)
    if is_app_relative(configured):
        return _SaveRoot(layout, reachable=False, caveats=(_app_relative_caveat(key, configured),))
    resolved = sandbox.host(key, configured)
    if resolved.path == configured:
        return _SaveRoot(layout)
    if resolved.path is None:
        return _SaveRoot(layout, reachable=False, caveats=resolved.caveats)
    return _SaveRoot(
        _dc_replace(layout, directory=resolved.path),
        sources=(f'{key} = "{configured}"{resolved.note}',),
    )


def _save_dir_probe(machine: Machine, sandbox: _Sandbox, key: str) -> Callable[[str], bool]:
    """``path_is_directory`` for one root read of *key*, host-side.

    RetroArch runs this test on every value it reads (``configuration.c:6920``
    for the saves root, ``:6941`` for the savestate one) and keeps the value
    only when it passes. A value atlas cannot test — one that exists only
    inside the Flatpak sandbox, one relative to the running executable's
    directory — passes: the emulator's own test still decides it, and answering
    "not a directory" here would reject a root that is very likely there. That
    the answer rests on an unperformed read is stated by the translation's own
    caveat.
    """

    def is_directory(value: str) -> bool:
        if is_app_relative(value):
            return True
        host = sandbox.host(key, value).path
        return host is None or machine.path_kind(host) == KIND_DIRECTORY

    return is_directory


def _rejected_dir_caveats(
    machine: Machine,
    sandbox: _Sandbox,
    rejected: Sequence[RejectedDirectory],
    *,
    key: str,
    effective: str,
) -> tuple[Caveat, ...]:
    """The roots the configs state and RetroArch refuses, layer by layer.

    ``path_is_directory`` failed, so that read set nothing
    (``configuration.c:6920-6932``, and the savestate twin ``:6941-6959``) and
    some other read decides the root. Which one is not fixed: usually the
    refusal is the last word and what stands preceded it — after an override,
    the global cfg's root rather than the platform default — but a refused
    global cfg can be followed by an override that supplies a usable root, and
    then the standing root was set afterwards. The message says whichever it
    was; claiming the wrong one would teach a reader a causality RetroArch does
    not have. The layer is named either way, because that is what tells a caller
    which file to fix.

    ``configured`` stays the cfg's own spelling — that is the line to edit —
    but inside a Flatpak that spelling is not where atlas looked. Where the two
    differ, the message names the host path too, so "not an existing directory"
    can be checked against the place the check was actually made.

    The code is the same for both families and the data is unchanged, because
    the caveat rides on the answer whose root it is about: a client reading it
    off a savestate placement is not left guessing which directory was refused,
    and a second code would have split one fact in two. *key* names the setting
    in the message, which is the part that would otherwise be wrong.
    """
    caveats: list[Caveat] = []
    for entry in rejected:
        stands = (
            f"writes to {effective!r} instead, the root a later file in the chain set"
            if entry.superseded
            else f"keeps {effective!r}, the root that stood before this file"
        )
        host = sandbox.host(key, entry.value).path
        looked_at = (
            f" (atlas looked at its host spelling {host!r})"
            if host is not None and host != entry.value
            else ""
        )
        caveats.append(
            Caveat(
                CAVEAT_INVALID_SAVE_DIRECTORY,
                f"{entry.layer}: {key} {entry.value!r} is not an existing "
                f"directory{looked_at} — RetroArch refuses it and {stands} "
                "(configuration.c:6914-6960)",
                {"layer": entry.layer, "configured": entry.value, "effective": effective},
            )
        )
        # When the rejection is a dead symlink, say why (REVIEW M7).
        if host is not None:
            caveats.extend(_link_view(machine, host)[1])
    return tuple(caveats)


def _unaudited_caveats(so_basename: str) -> tuple[Caveat, ...]:
    """What the audit says about a core that carries no rule card (REVIEW H7).

    A missing card is not evidence the standard rule is complete — the verdict
    decides how loudly to say so.
    """
    caveats: list[Caveat] = []
    short_name = so_basename.removesuffix(".so").removesuffix("_libretro")
    verdict_entry = lookup_audit(short_name)
    if verdict_entry is None:
        caveats.append(
            Caveat(
                CAVEAT_CORE_UNAUDITED,
                f"core {short_name!r} has not been audited — the standard rule is assumed, "
                "not verified (docs/research/coverage-matrix.md)",
                {"core": short_name},
            )
        )
    elif verdict_entry.verdict == "suspect":
        caveats.append(
            Caveat(
                CAVEAT_CORE_SUSPECT,
                f"core {short_name!r} is a documented deviation suspect — this standard answer "
                "may miss an additional or different save stack (docs/research/core-audit.md)",
                {"core": short_name, "verdict": verdict_entry.verdict},
            )
        )
    elif verdict_entry.verdict == "multi-option":
        # The directory is established; the granularity is not, and an
        # empty `granularity` field alone reads as nothing-to-report
        # (issue #23). The audit knows which options decide it, so the
        # answer names them instead of leaving the caller with "unknown".
        options = ", ".join(verdict_entry.save_options)
        caveats.append(
            Caveat(
                CAVEAT_CORE_MULTI_OPTION,
                f"core {short_name!r} places its saves in this directory, but its file set and "
                f"granularity depend on core options atlas does not interpret ({options}) — the "
                "granularity here is unstated, not standard (docs/research/core-audit.md)",
                {"core": short_name, "verdict": verdict_entry.verdict, "options": options},
            )
        )
    return tuple(caveats)


@dataclass(frozen=True, slots=True)
class _OptionGates:
    """Which options files RetroArch would consult, and what reading that cost.

    The preamble every route that reads a core option shares. Both flags
    default from ``config.def.h`` and both are read from the MERGED config,
    after ``config_load_override`` has run: the core's own
    ``retro_set_environment`` (``runloop.c:5037``) triggers
    ``runloop_init_core_options``, which reads
    ``settings->bools.game_specific_options`` and ``.global_core_options``
    (``runloop.c:1529-1530``, ``:1564-1565``) — one step after the overrides
    were merged at ``:5003``. So an override that says
    ``game_specific_options = "false"`` really does switch the game/folder
    ``.opt`` layer off, unlike ``auto_overrides_enable``, which is captured
    before the merge (``:4941``).

    Shared rather than written twice because the second copy had already lost
    this comment: the save route reads its card's governing option here and the
    texture route reads its replacement switch, and neither may drift from the
    order RetroArch itself walks.
    """

    global_file: str
    game_specific_options: bool
    per_core_options: bool
    caveats: tuple[Caveat, ...]


def _option_gates(
    layers: Sequence[_CfgLayer], *, sandbox: _Sandbox, retroarch_config_dir: str
) -> _OptionGates:
    """Read the two gates and locate the global options file, once."""
    global_file, options_file_caveats = _global_options_file(
        layers, sandbox=sandbox, retroarch_config_dir=retroarch_config_dir
    )
    global_core_options, global_ignored = chain_bool(layers, "global_core_options", default=False)
    game_specific_options, game_ignored = chain_bool(layers, "game_specific_options", default=True)
    return _OptionGates(
        global_file=global_file,
        game_specific_options=game_specific_options,
        per_core_options=not global_core_options,
        caveats=(*options_file_caveats, *_ignored_caveats((*global_ignored, *game_ignored))),
    )


@dataclass(frozen=True, slots=True)
class _CardChoice:
    """The rule card that applies here, once feature detection has had its say.

    ``live_option`` is the governing option as the core *registers* it — the
    observation that confirms the card's generation. ``live_options`` is the
    rule-card plural: every declared rule option the core registers, or
    ``None`` where the options could not be captured at all (a probe
    limitation, not a mismatch).
    """

    card: CoreCard | None = None
    live_option: CoreOption | None = None
    live_options: Mapping[str, CoreOption] | None = None
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


def _select_card(*, so_basename: str | None, core_info: CoreInfo | None) -> _CardChoice:
    """Which rule card applies to this core — decided on evidence, not on a version.

    Feature detection is the generation question made observable (the LRPS2
    lesson), and ``core_info`` carries the three answers the probe can give,
    which this function must keep apart (issue #81):

    * ``None`` — the core could not be examined at all: the ``.so`` is absent or
      unreadable, built for another architecture, or missing a host library.
      Nothing about the installed core was established, and selecting a card on
      the ``.so`` file name alone would state a recorded deviation as though its
      generation had been confirmed. The card is NOT applied.
    * options captured — the card's governing option key decides. Key registered
      → the generation is confirmed by evidence. Key not registered → the card
      describes a different generation and is NOT applied; stale knowledge with
      a warning would still be a guess.
    * options not captured (``CoreInfo.options is None`` — a probe limitation:
      LRPS2 and NeoCD register theirs after atlas listens) → unknown. The core
      itself answered, so the card applies and the version comparison keeps
      doing its job.

    Only the second state can retire a card for describing another generation;
    the first never saw a generation to compare. That is why they carry separate
    codes and can never ride together.
    """
    library_name = core_info.library_name if core_info is not None else None
    card = lookup_card(so_basename=so_basename, library_name=library_name)
    choice = _CardChoice(card=card)
    if card is not None:
        if core_info is None:
            choice = _CardChoice(
                caveats=(
                    Caveat(
                        CAVEAT_CORE_GENERATION_UNESTABLISHED,
                        f"core {card.key!r} is recorded as placing its saves outside the standard "
                        "layout, but the installed core could not be read — which generation is "
                        "here was never established, so the recorded behaviour is not applied; "
                        "the standard answer below may miss the real save stack",
                        {"core": card.key},
                    ),
                )
            )
        elif card.option_key is not None and core_info.options is not None:
            choice = _option_confirmed_choice(card, core_info.options)
        elif card.rule_options and core_info.options is not None:
            choice = _rule_confirmed_choice(card, core_info.options)
    if choice.card is None and so_basename is not None:
        choice = _dc_replace(
            choice, caveats=(*choice.caveats, *_unaudited_caveats(so_basename))
        )
    return choice


def _option_confirmed_choice(card: CoreCard, registered: Mapping[str, CoreOption]) -> _CardChoice:
    """Feature detection for a single-option card: the key registered, or not."""
    live_option = registered.get(card.option_key or "")
    if live_option is None:
        return _CardChoice(
            caveats=(
                Caveat(
                    CAVEAT_CORE_GENERATION_MISMATCH,
                    f"core {card.key!r} is recorded as placing its saves outside the standard "
                    f"layout under option {card.option_key!r}, which this core does not "
                    "register — the recorded behaviour belongs to a different core generation "
                    "and is not applied; this core's actual save behaviour is unknown until "
                    "re-audited, so the standard answer below may miss the real save stack",
                    {"core": card.key, "option_key": card.option_key or ""},
                ),
            )
        )
    return _CardChoice(
        card=card,
        live_option=live_option,
        # The full registration travels too: an observation gate reads
        # switches beyond the governing one, and their registered defaults
        # are live facts exactly like the governing option's (issue #89).
        live_options=registered,
        sources=(
            f"feature-detected: core registers {card.option_key!r} (default "
            f"{live_option.default!r}, values {list(live_option.values)}) — card generation "
            "confirmed by observation, not by version comparison",
        ),
    )


def _rule_confirmed_choice(card: CoreCard, registered: Mapping[str, CoreOption]) -> _CardChoice:
    """The rule-card plural: every declared option registered, or the card retires.

    A single missing switch is the same generation mismatch a single-option
    card answers with — the rule would be reading a switch this core does not
    have.
    """
    missing = [key for key in card.rule_options or () if key not in registered]
    if missing:
        return _CardChoice(
            caveats=(
                Caveat(
                    CAVEAT_CORE_GENERATION_MISMATCH,
                    f"core {card.key!r} is recorded as selecting between save behaviours by "
                    f"options this core does not register ({', '.join(missing)}) — the "
                    "recorded behaviour belongs to a different core generation and is not "
                    "applied; this core's actual save behaviour is unknown until re-audited, "
                    "so the standard answer below may miss the real save stack",
                    {"core": card.key, "options": ", ".join(missing)},
                ),
            )
        )
    return _CardChoice(
        card=card,
        live_options={key: registered[key] for key in card.rule_options or ()},
        sources=(
            f"feature-detected: core registers {', '.join(card.rule_options or ())} — card "
            "generation confirmed by observation, not by version comparison",
        ),
    )


def _version_drift(
    verified: VerifiedOn, *, arrangement_version: str | None, core_version: str | None
) -> tuple[dict[str, str], list[str]]:
    """Which pinned versions differ here, and which this machine does not expose."""
    drift: dict[str, str] = {}
    missing: list[str] = []
    if verified.version is not None:
        if arrangement_version is None:
            missing.append("arrangement_version")
        elif verified.version != arrangement_version:
            drift["arrangement_verified"] = verified.version
            drift["arrangement_live"] = arrangement_version
    if verified.core_library_version is not None:
        if core_version is None:
            missing.append("core_library_version")
        elif verified.core_library_version != core_version:
            drift["core_verified"] = verified.core_library_version
            drift["core_live"] = core_version
    return drift, missing


def _verification_notes(
    card: CoreCard,
    *,
    arrangement: str,
    arrangement_version: str | None,
    core_version: str | None,
    feature_confirmed: bool,
) -> tuple[tuple[str, ...], tuple[Caveat, ...]]:
    """Was this card verified on this arrangement, and does the record still hold?

    Explicit and failing closed (REVIEW M3): the states are verified, drifted,
    runtime-version-unknown, never-verified — missing live evidence is never
    treated as successful verification.
    """
    audit = lookup_audit(card.key)
    verified = audit.verified.get(arrangement) if audit is not None else None
    if verified is None:
        return (), (
            Caveat(
                CAVEAT_UNVERIFIED_VERSION,
                f"the recorded save behaviour of core {card.key!r} was never verified on a "
                f"{arrangement} arrangement — the behaviour it describes may not hold here",
                {"core": card.key, "arrangement": arrangement, "verification": "never-verified"},
            ),
        )
    drift, missing = _version_drift(
        verified, arrangement_version=arrangement_version, core_version=core_version
    )
    if (drift or missing) and feature_confirmed:
        # Feature detection outranks the version comparison: the governing
        # option is observably registered, so a differing or unreadable version
        # record is supplementary info, not an alarm (the false-alarm class the
        # version check produced).
        detail = str(drift) if drift else f"{', '.join(missing)} unavailable"
        return (
            f"rule card '{card.key}': version records differ from this machine ({detail}), but "
            f"the governing option is feature-confirmed — the decision falls on observed evidence",
        ), ()
    if drift:
        data = {"core": card.key, "arrangement": arrangement, "verification": "drifted", **drift}
        if missing:
            data["missing"] = ", ".join(missing)
        return (), (
            Caveat(
                CAVEAT_UNVERIFIED_VERSION,
                f"the recorded save behaviour of core {card.key!r} was verified against different "
                f"versions than this machine runs ({drift}) — behaviour may have drifted",
                data,
            ),
        )
    if missing:
        return (), (
            Caveat(
                CAVEAT_UNVERIFIED_VERSION,
                f"the recorded save behaviour of core {card.key!r} is pinned to {arrangement} "
                f"versions this machine does not expose ({', '.join(missing)} unavailable) — the "
                "verification cannot be confirmed live",
                {
                    "core": card.key,
                    "arrangement": arrangement,
                    "verification": "runtime-version-unknown",
                    "missing": ", ".join(missing),
                },
            ),
        )
    return (
        f"rule card '{card.key}': verified on {arrangement} "
        f"{verified.version or '?'} (core {verified.core_library_version or '?'}, "
        f"{verified.date or 'undated'})",
    ), ()


def _no_governing_value(card: CoreCard) -> Caveat:
    """Nothing on this machine, and nothing in the core, says which mode is active.

    The card fits the core — its option key is the one this generation registers,
    or the read was inconclusive and the version comparison still stands — but the
    value that selects between its modes was never established: no options file
    states it, and the core declared no default to fall back on. Picking a mode
    from the card's order, or from the first one written down, would be the guess
    the boundary rule exists to prevent, so the card steps aside exactly as it
    does for a generation nobody could confirm.
    """
    return Caveat(
        CAVEAT_CORE_OPTION_VALUE_UNESTABLISHED,
        f"core {card.key!r} is recorded as placing its saves outside the standard layout under "
        f"option {card.option_key!r}, and which value governs it here was never established — no "
        "configuration on this machine states one and the installed core declared no default, so "
        "the recorded behaviour is not applied; the standard answer below may miss the real save "
        "stack",
        {"core": card.key, "option_key": card.option_key or ""},
    )


def _mode_for_unknown_value(
    card: CoreCard, *, opt_value: str, effective_default: str | None, live_option: CoreOption | None
) -> tuple[CoreCard | None, SaveMode | None, str, Caveat]:
    """What applies when the configured option value has no mode on the card.

    Either the live core legitimately offers a value the card does not know, or
    even the effective default has no card mode — value-level generation drift,
    and applying any other mode would guess, so the card steps aside. Otherwise
    RetroArch's option manager keeps the core-declared default when a persisted
    value is invalid; it does not fall back to the standard rule (REVIEW M1).

    A third way to have no mode is to have no default at all: RetroArch would
    keep the core's own, and neither the core nor the record states it. Nothing
    about the card's generation is wrong there, so it is not the mismatch — it is
    the setting that was never read (:func:`_no_governing_value`).
    """
    live_registered_value = live_option is not None and opt_value in live_option.values
    fallback_mode = card.modes.get(effective_default or "")
    if fallback_mode is None and effective_default is None and not live_registered_value:
        return None, None, opt_value, _no_governing_value(card)
    if live_registered_value or fallback_mode is None:
        return (
            None,
            None,
            opt_value,
            Caveat(
                CAVEAT_CORE_GENERATION_MISMATCH,
                f'core option {card.option_key} = "{opt_value}" is a value the recorded save '
                f"behaviour of core {card.key!r} cannot interpret — the record lags this core's "
                "generation; the configured save behaviour is unknown until re-audited, and the "
                "standard answer below may miss the real save stack",
                {"core": card.key, "option_key": card.option_key or "", "value": opt_value},
            ),
        )
    return (
        card,
        fallback_mode,
        effective_default or opt_value,
        Caveat(
            CAVEAT_UNKNOWN_OPTION_VALUE,
            f'core option {card.option_key} = "{opt_value}" is not a value the recorded save '
            f"behaviour knows — applying the core default mode {effective_default!r} as RetroArch would",
            {"core": card.key, "option_key": card.option_key or "", "value": opt_value},
        ),
    )


@dataclass(frozen=True, slots=True)
class _CardApplication:
    """The card as it applies here: which mode governs, and what that makes granular.

    ``card`` comes back ``None`` when the card stepped aside — the answer then
    falls through to the standard rule, with the mismatch stated.
    ``excluded_observations`` are observe candidates a live switch rules out
    (an observation gate, issue #89): names the mode would have probed blind
    and this machine's configuration says cannot exist.
    """

    card: CoreCard | None = None
    mode: SaveMode | None = None
    granularity: Granularity | None = None
    caveats: tuple[Caveat, ...] = ()
    excluded_observations: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Observation gates — a mode's observe candidates refined by switches the card
# does not select its modes with (issue #89). A gate is code keyed by (card,
# mode), the same split the selection rules make: the card states what *can*
# exist, the gate reads what this machine's switches rule out, and the reason
# rides the answer as readings. A gate only ever *removes* candidates, and
# only on an established value — a switch nobody could read excludes nothing,
# because "cannot exist" is a claim, not a default.
# ---------------------------------------------------------------------------

# flycast@1dac369: a slot-2 VMU file exists only while the port's expansion
# slot holds the VMU device — reicast_device_port{1..4}_slot2 registers
# VMU|Purupuru|None with default Purupuru (libretro_core_options.h:995-1008),
# and only the literal "VMU" maps to MDT_SegaVMU (libretro.cpp:996-1010), so
# on a default machine no vmu_save_<port>2.bin can appear. The main-device
# options can only *further* forbid a slot (a non-controller device forces
# both expansions to None, libretro.cpp:990-993, :1013-1017), so excluding on
# the slot option alone never rules out a file that could exist.
_FLYCAST_SLOT2_PORTS = tuple(zip("1234", "ABCD"))


def _flycast_slot2_gate(
    read_option: "Callable[[str], OptionReading]",
) -> tuple[frozenset[str], tuple[OptionReading, ...]]:
    excluded: set[str] = set()
    readings: list[OptionReading] = []
    for port, letter in _FLYCAST_SLOT2_PORTS:
        reading = read_option(f"reicast_device_port{port}_slot2")
        readings.append(reading)
        if reading.value is not None and reading.value != "VMU":
            excluded.add(f"vmu_save_{letter}2.bin")
    return frozenset(excluded), tuple(readings)


_OBSERVATION_GATES: Mapping[
    tuple[str, str],
    "Callable[[Callable[[str], OptionReading]], tuple[frozenset[str], tuple[OptionReading, ...]]]",
] = {("flycast", "disabled"): _flycast_slot2_gate}


def _retired_entry_caveats(
    card: CoreCard, found: tuple[tuple[RetiredOption, str], ...], options_file: str
) -> tuple[Caveat, ...]:
    """Each retired entry the governing options file still carries, stated (issue #79).

    The inverse direction of ``core-generation-mismatch``: there the record
    names a key the core does not register and the card steps aside; here the
    *file* names one, the answer stands on the current key, and this names
    the dead entry — the value someone set there silently stopped applying,
    because RetroArch never prunes the file and the core simply stopped
    reading the key.
    """
    return tuple(
        Caveat(
            CAVEAT_OPTION_ENTRY_RETIRED,
            f'options entry {option.key} = "{value}" no longer applies: the {card.key!r} '
            f"generation shipped here does not read that key, so the value set in "
            f"{os.path.basename(options_file)} silently stopped applying and the current "
            f"option decides instead — {option.citation}",
            {
                "core": card.key,
                "option_key": option.key,
                "value": value,
                "options_file": options_file,
            },
        )
        for option, value in found
    )


def _apply_card(
    machine: Machine,
    *,
    sandbox: _Sandbox,
    retroarch_config_dir: str,
    cfg_label: str,
    layout: RetroArchCfg,
    card: CoreCard,
    live_option: CoreOption | None,
    live_options: Mapping[str, CoreOption] | None,
    library_name: str | None,
    layers: Sequence[_CfgLayer],
    content: _Content,
    gates: _OverrideGates,
) -> _CardApplication:
    """Read the option that governs this card, live, and take the mode it selects.

    A card without a governing option states one fixed behaviour. Otherwise the
    registered default is a live read and outranks the card's shipped-generation
    copy — feature detection makes option defaults machine facts instead of
    world knowledge.

    The options files are resolved here rather than per query because this is
    where they are read: a card that governs nothing consults none of them, and
    an unreachable ``core_options_path`` is only a degradation for an answer
    that would have looked there. A card governed by a rule takes its own
    route (:func:`_apply_rule_card`): the rule reads several switches, or none
    at all, and what it needs beyond the options files is threaded there.
    """
    if card.rule_options is not None:
        return _apply_rule_card(
            machine,
            sandbox=sandbox,
            retroarch_config_dir=retroarch_config_dir,
            cfg_label=cfg_label,
            layout=layout,
            card=card,
            live_options=live_options,
            library_name=library_name,
            layers=layers,
            content=content,
            gates=gates,
        )
    if card.option_key is None:
        # The load refuses any other shape (_expect_selectable_modes), so the
        # mode is there — a card that governs nothing states exactly this one.
        mode = card.modes[MODE_ALWAYS]
        return _CardApplication(
            card=card,
            mode=mode,
            granularity=Granularity(
                value=mode.granularity,
                mode=MODE_ALWAYS,
                readings=(),
                alternatives=(),
                provenance=f"rule card '{card.key}': fixed behaviour (no governing option)",
            ),
        )

    effective_default = card.option_default
    if live_option is not None and live_option.default is not None:
        effective_default = live_option.default
    option_gates = _option_gates(
        layers, sandbox=sandbox, retroarch_config_dir=retroarch_config_dir
    )
    opt_value, opt_source, options_file, retired_found = _core_options_value(
        machine,
        override_config_dir=gates.override_config_dir,
        global_file=option_gates.global_file,
        library_name=library_name,
        content_dir_name=content.dir_name,
        rom_stem=content.rom_stem,
        option_key=card.option_key or "",
        option_default=effective_default,
        game_specific_options=option_gates.game_specific_options,
        per_core_options=option_gates.per_core_options,
        retired=card.retired_options,
    )
    caveats: list[Caveat] = [
        *option_gates.caveats,
        *_retired_entry_caveats(card, retired_found, options_file),
    ]
    if opt_value is None:
        # No file states the option and no default was established. There is
        # nothing to select a mode with, and no granularity to report either:
        # the field says which grouping is in force, and none is known.
        #
        # Deleting this branch does not change a single answer — the general
        # one in _mode_for_unknown_value reaches the same caveat for a value of
        # None. What it does is let None past a `str`, and the type check is
        # what says so; keep the two apart rather than widening the parameter,
        # because "no value was read" and "this value selects no mode" are
        # different questions that happen to have one answer here.
        caveats.append(_no_governing_value(card))
        return _CardApplication(caveats=tuple(caveats))
    applied: CoreCard | None = card
    mode = card.modes.get(opt_value)
    if mode is None:
        applied, mode, opt_value, caveat = _mode_for_unknown_value(
            card, opt_value=opt_value, effective_default=effective_default, live_option=live_option
        )
        caveats.append(caveat)
    excluded: frozenset[str] = frozenset()
    gate_readings: tuple[OptionReading, ...] = ()
    gate = _OBSERVATION_GATES.get((card.key, opt_value)) if applied is not None else None
    if gate is not None:

        def _read_gate_option(key: str) -> OptionReading:
            live = (live_options or {}).get(key)
            value, source, gate_file, _ = _core_options_value(
                machine,
                override_config_dir=gates.override_config_dir,
                global_file=option_gates.global_file,
                library_name=library_name,
                content_dir_name=content.dir_name,
                rom_stem=content.rom_stem,
                option_key=key,
                option_default=live.default if live is not None else None,
                game_specific_options=option_gates.game_specific_options,
                per_core_options=option_gates.per_core_options,
            )
            return OptionReading(key, value, source, gate_file)

        excluded, gate_readings = gate(_read_gate_option)
    granularity = None
    if applied is not None and mode is not None:
        granularity = Granularity(
            value=mode.granularity,
            mode=opt_value,
            readings=(
                OptionReading(card.option_key, opt_value, opt_source, options_file),
                *gate_readings,
            ),
            alternatives=tuple(
                ModeAlternative(
                    mode=value,
                    options=((card.option_key, value),),
                    values=other.granularities,
                )
                for value, other in card.modes.items()
                if value != opt_value
            ),
            provenance=opt_source,
        )
    return _CardApplication(
        card=applied,
        mode=mode,
        granularity=granularity,
        caveats=tuple(caveats),
        excluded_observations=excluded,
    )


class _ConsultedOptions(Mapping[str, "str | None"]):
    """The rule's view of its options — recording which ones it actually read.

    The answer's readings are the switches that decided the mode *here*, not
    every switch the card declares: hatari reads one of its two write-protect
    options, because which one governs is the content's class, and listing the
    other would tell a caller a switch mattered that did not. Reading a key
    the card never declared is a build mistake and raises as one.
    """

    def __init__(self, values: dict[str, str | None]) -> None:
        self._values = values
        self.consulted: list[str] = []

    def __getitem__(self, key: str) -> str | None:
        if key not in self._values:
            raise ValueError(
                f"a mode-selection rule read option {key!r}, which its card does not declare — "
                "the rule and the card shipped out of step"
            )
        if key not in self.consulted:
            self.consulted.append(key)
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _rule_option_readings(
    machine: Machine,
    *,
    card: CoreCard,
    live_options: Mapping[str, CoreOption] | None,
    option_gates: _OptionGates,
    library_name: str | None,
    content: _Content,
    gates: _OverrideGates,
) -> tuple[dict[str, str | None], dict[str, OptionReading], list[Caveat]]:
    """Every declared rule option, read live and normalized against the core.

    The normalization is RetroArch's own move made explicit: a persisted value
    outside the registered set is kept as the core's default by the option
    manager, so the rule sees what the core would run with — plus the caveat
    that says a stored value was passed over (the single-option route states
    the same thing through :func:`_mode_for_unknown_value`).
    """
    values: dict[str, str | None] = {}
    readings: dict[str, OptionReading] = {}
    caveats: list[Caveat] = []
    for key in card.rule_options or ():
        live = (live_options or {}).get(key)
        default = live.default if live is not None else None
        value, source, options_file, retired_found = _core_options_value(
            machine,
            override_config_dir=gates.override_config_dir,
            global_file=option_gates.global_file,
            library_name=library_name,
            content_dir_name=content.dir_name,
            rom_stem=content.rom_stem,
            option_key=key,
            option_default=default,
            game_specific_options=option_gates.game_specific_options,
            per_core_options=option_gates.per_core_options,
            # The candidate chain is key-independent, so every key reads the
            # same governing file — the retired sweep rides the first read
            # and the others skip it rather than stating each entry N times.
            retired=card.retired_options if not readings else (),
        )
        caveats.extend(_retired_entry_caveats(card, retired_found, options_file))
        if (
            value is not None
            and live is not None
            and value not in live.values
            and default is not None
        ):
            caveats.append(
                Caveat(
                    CAVEAT_UNKNOWN_OPTION_VALUE,
                    f'core option {key} = "{value}" is not a value the installed core registers '
                    f"— applying the core default {default!r} as RetroArch would",
                    {"core": card.key, "option_key": key, "value": value},
                )
            )
            value = default
            source = f'core default: {key} = "{default}" (the stored value is not one the core registers)'
        values[key] = value
        readings[key] = OptionReading(key, value, source, options_file)
    return values, readings, caveats


def _rule_file_lookup(machine: Machine, base: str | None, name: str) -> FileLookup:
    """One file a rule asked for under *base* — unreadable when the root is out of reach."""
    if base is None:
        return FileLookup(None, FILE_UNREADABLE, None)
    path = os.path.join(base, name)
    result = machine.read_text(path)
    if result.status == READ_OK:
        return FileLookup(result.text, FILE_READ, path)
    if result.status == READ_MISSING:
        return FileLookup(None, FILE_ABSENT, path)
    return FileLookup(None, FILE_UNREADABLE, path)


def _rule_entries(machine: Machine, base: str | None, name: str) -> tuple[str, ...] | None:
    """The names in one directory a rule asked about — ``None`` when unlistable.

    An absent directory answers ``()``, a truthful negative; a root that is
    itself out of reach answers ``None`` the way an unlistable directory does,
    because a rule that cannot look must not conclude nothing is there.
    """
    if base is None:
        return None
    listing = machine.glob(os.path.join(_glob_escape(os.path.join(base, name)), "*"))
    if listing.status != GLOB_COMPLETE:
        return None
    return tuple(os.path.basename(match) for match in listing.matches)


def _rule_reading(
    machine: Machine,
    *,
    values: dict[str, str | None],
    sandbox: _Sandbox,
    cfg_label: str,
    layout: RetroArchCfg,
    layers: Sequence[_CfgLayer],
    content: _Content,
    retroarch_config_dir: str,
) -> tuple[RuleReading, _ConsultedOptions]:
    """The machine, packaged for one rule: options, content class, files, paths.

    Everything here is a read of the running machine or a closure over one —
    the rule itself never touches the machine seam directly, so what a rule
    *can* decide on stays enumerable in one place.
    """

    def _system_base() -> str | None:
        root = _core_system_root(
            sandbox=sandbox,
            cfg_label=cfg_label,
            layers=layers,
            content=content,
            retroarch_config_dir=retroarch_config_dir,
        )
        if root.needs or not root.reachable:
            return None
        return root.base

    # The home the emulator's own ``$HOME`` expands to is the sandbox
    # environment's HOME, which is shared with the host — so a rule's
    # home-relative read follows the emulator's expansion, not atlas's.
    def system_file(name: str) -> FileLookup:
        return _rule_file_lookup(machine, _system_base(), name)

    def home_file(name: str) -> FileLookup:
        return _rule_file_lookup(machine, sandbox.expansion_home, name)

    def system_entries(name: str) -> tuple[str, ...] | None:
        return _rule_entries(machine, _system_base(), name)

    def home_entries(name: str) -> tuple[str, ...] | None:
        return _rule_entries(machine, sandbox.expansion_home, name)

    def is_directory(path: str) -> bool | None:
        resolved = sandbox.host("savepath", path)
        if resolved.path is None:
            return None
        return machine.path_kind(resolved.path) == KIND_DIRECTORY

    save_dirs: list[str] = []
    if layout.directory is not None:
        # The spellings retro_get_save_dir can have handed the core: the
        # configured root, and the sorted directory RetroArch redirects to —
        # a rule comparing a persisted path against "the save directory"
        # must recognize either.
        save_dirs.append(layout.directory)
        for segment in (content.dir_name, content.rom_stem):
            if segment:
                save_dirs.append(os.path.join(layout.directory, segment))
    recorder = _ConsultedOptions(values)
    return (
        RuleReading(
            option_values=recorder,
            content_extension=content.extension,
            content_stem=content.rom_stem,
            system_file=system_file,
            home_file=home_file,
            system_entries=system_entries,
            home_entries=home_entries,
            save_dirs=tuple(save_dirs),
            is_directory=is_directory,
        ),
        recorder,
    )


def _apply_rule_card(
    machine: Machine,
    *,
    sandbox: _Sandbox,
    retroarch_config_dir: str,
    cfg_label: str,
    layout: RetroArchCfg,
    card: CoreCard,
    live_options: Mapping[str, CoreOption] | None,
    library_name: str | None,
    layers: Sequence[_CfgLayer],
    content: _Content,
    gates: _OverrideGates,
) -> _CardApplication:
    """Hand the machine to the card's selection rule and take the mode it names.

    The rule decides, the resolver reads: every option the card declares is
    read live first, and the rule sees the values through a recorder so the
    answer's readings are exactly the switches that went into the decision.
    A rule that cannot decide returns no mode with the reason as caveats, and
    the card steps aside the way it does for an unconfirmed generation. A
    rule naming a mode its card does not state is a build mistake — the card
    and the rule ship together — and fails loudly rather than resolving
    wrongly.
    """
    option_gates = _option_gates(layers, sandbox=sandbox, retroarch_config_dir=retroarch_config_dir)
    values, readings, caveats = _rule_option_readings(
        machine,
        card=card,
        live_options=live_options,
        option_gates=option_gates,
        library_name=library_name,
        content=content,
        gates=gates,
    )
    caveats = [*option_gates.caveats, *caveats]
    reading, recorder = _rule_reading(
        machine,
        values=values,
        sandbox=sandbox,
        cfg_label=cfg_label,
        layout=layout,
        layers=layers,
        content=content,
        retroarch_config_dir=retroarch_config_dir,
    )
    choice = MODE_RULES[card.key](reading)
    caveats.extend(choice.caveats)
    if choice.mode is None:
        return _CardApplication(caveats=tuple(caveats))
    mode = card.modes.get(choice.mode)
    if mode is None:
        raise ValueError(
            f"the rule for card {card.key!r} selected mode {choice.mode!r}, which the card does "
            "not state — the rule and the card shipped out of step"
        )
    unknown = [name for name, _ in choice.alternatives if name not in card.modes]
    if unknown:
        raise ValueError(
            f"the rule for card {card.key!r} offered alternatives {unknown}, which the card does "
            "not state — the rule and the card shipped out of step"
        )
    consulted = tuple(readings[key] for key in recorder.consulted) + choice.readings
    granularity = Granularity(
        value=mode.granularity,
        mode=choice.mode,
        readings=consulted,
        alternatives=tuple(
            ModeAlternative(mode=name, options=combo, values=card.modes[name].granularities)
            for name, combo in choice.alternatives
        ),
        provenance=(
            f"rule card '{card.key}': mode {choice.mode!r} selected by the card's rule from "
            + (
                ", ".join(f'{r.key} = "{r.value}"' for r in consulted if r.value is not None)
                or "the card's fixed knowledge"
            )
        ),
    )
    return _CardApplication(card=card, mode=mode, granularity=granularity, caveats=tuple(caveats))


def _card_file_set(
    machine: Machine,
    *,
    card: CoreCard,
    mode: SaveMode,
    directory: str,
    rom_stem: str | None,
    content_dir_name: str | None,
    observable: bool,
    excluded: frozenset[str] = frozenset(),
) -> FileSet:
    """What the card says lies in its own directory — declared, or observed there.

    A template file that cannot be filled leaves the set honestly unknown; with
    a hole in the names there is nothing to look for, and where the directory
    itself is a hole or a path this host cannot reach there is nowhere to look
    (*observable* false), so the declaration stands unobserved.
    """
    declared = _card_files(mode.files, rom_stem) if mode.files is not None else None
    if declared is None:
        return UNKNOWN_FILE_SET
    groups = _declared_groups(
        mode, directory=directory, rom_stem=rom_stem or "", content_dir_name=content_dir_name
    )
    if not observable or file_set_holes(declared):
        return FileSet(
            "declared",
            declared,
            f"declared by rule card '{card.key}'",
            complete=mode.complete,
            groups=groups,
        )
    # Observation candidates may be wider than the declared defaults —
    # e.g. Flycast's slot-2 VMUs exist only when configured (REVIEW M2).
    # An observation gate narrows them back where a live switch says a
    # candidate cannot exist here (issue #89): a stale file under an excluded
    # name is not part of what this configuration reads or writes.
    observe = _card_files(mode.observe, rom_stem) if mode.observe is not None else None
    candidates = tuple(f for f in (observe if observe is not None else declared) if f not in excluded)
    present = tuple(f for f in candidates if machine.path_kind(os.path.join(directory, f)) == KIND_FILE)
    if present:
        return FileSet("observed", present, f"observed on the machine: {directory}", complete=mode.complete)
    return FileSet(
        "declared",
        declared,
        f"declared by rule card '{card.key}' (none present yet)",
        complete=mode.complete,
        groups=groups,
    )


def _file_set_caveats(
    card: CoreCard, mode: SaveMode, *, mode_value: str, rom_stem: str | None
) -> tuple[Caveat, ...]:
    """What one declared file list cannot say about this mode's save.

    Two states the card keeps apart, each stated rather than left to an
    empty-looking answer: a mode whose names depend on a fact atlas does not
    read (both spellings are handed over, the caller picks), and a mode whose
    names are not established yet. The spanning mode stopped being a third:
    its cross-root parts state their files as groups now, and
    :func:`_cross_root_parts` carries them.
    """
    if mode.files is None:
        return (
            Caveat(
                CAVEAT_FILENAMES_UNVERIFIED,
                f"core {card.key!r} in mode {mode_value!r} places per-game files under the standard "
                "directory, but the filename scheme is unverified — file names not stated",
                {"core": card.key, "mode": mode_value},
            ),
        )
    if mode.files_without_save_id is not None or mode.files_established_for is not None:
        stated = _card_files(mode.files, rom_stem) or mode.files
        data = {"core": card.key, "mode": mode_value, "files": ", ".join(stated)}
        spelling = ""
        scope = ""
        if mode.files_without_save_id is not None:
            alternative = _card_files(mode.files_without_save_id, rom_stem) or mode.files_without_save_id
            data["files_without_save_id"] = ", ".join(alternative)
            spelling = (
                " The names hold for content that carries a platform-native id; content without one "
                "is named after the ROM instead, and that spelling is in this caveat's data — "
                "whoever fills 'save_id' knows which applies."
            )
        if mode.files_established_for is not None:
            data["files_established_for"] = mode.files_established_for
            scope = (
                f" Which files exist at all was established for {mode.files_established_for} content "
                "only: another content class connects a different set of devices, so a name stated "
                "here may never appear for it, and one that does may be missing."
            )
        if mode.files_citation is not None:
            data["citation"] = mode.files_citation
        return (
            Caveat(
                CAVEAT_FILENAMES_CONTENT_CONDITIONAL,
                f"core {card.key!r} in mode {mode_value!r}: the file set depends on the content, "
                f"which atlas does not identify.{spelling}{scope}",
                data,
            ),
        )
    return ()


def _unnamed_tree_caveats(
    card: CoreCard,
    mode: SaveMode,
    *,
    directory: str,
    rom_stem: str | None,
    content_dir_name: str | None,
) -> tuple[Caveat, ...]:
    """Directories this mode writes into whose file names atlas cannot derive.

    Named rather than left out: a client that skips the directory loses whatever
    is in it, and for MAME's differencing images that is the player's progress on
    every machine with a hard disk. The names stay unstated because they come
    from the machine's own ROM table inside the binary, which is no read of *this*
    machine — and the card's reason for saying so travels as the citation.
    """
    base = _base_of(directory, mode.subdir)
    caveats = []
    for group in mode.unnamed:
        subdir, _ = _fill_subdir(
            group.subdir or "", rom_stem=rom_stem, content_dir_name=content_dir_name
        )
        where = os.path.join(base, *[s for s in subdir.split("/") if s])
        caveats.append(
            Caveat(
                CAVEAT_FILE_NAMES_UNESTABLISHED,
                f"core {card.key!r} keeps files under {where} whose names do not follow "
                "from anything atlas reads — the directory is stated, the names are not, and "
                "the citation says what stands behind them",
                {"core": card.key, "dir": where, "role": group.role, "citation": group.unnamed or ""},
            )
        )
    return tuple(caveats)


@dataclass(frozen=True, slots=True)
class _SystemRoot:
    """The directory RetroArch hands a core that asks for the system directory.

    ``base`` is that directory, or the ``<content_dir>`` template when the
    content's own directory is the root and the caller named no content.
    ``root_kind`` says which of the two anchors it is: the key's value is not
    always where the core is sent, so a card rooted in the system directory can
    legitimately answer ``content_directory``. ``reachable`` is false for a
    configured spelling with no host location — the emulator writes there, but
    nothing below it can be looked at from here.
    """

    base: str
    root_kind: RootKind
    needs: tuple[str, ...] = ()
    reachable: bool = True
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


def _content_system_root(content: _Content, *, provenance: str) -> _SystemRoot:
    """The content's own directory as the system root — resolved, or left as a hole.

    With no content loaded at all, upstream hands back the standing
    ``system_directory`` (``runloop.c:1986-1987``) — empty where an emptied key
    led into this branch, the configured directory where the flag alone did.
    Neither is an answer about a save, because a save presupposes content, so a
    caller who names none is answered with the template instead, exactly as
    ``savefiles_in_content_dir`` is answered on the standard route — and
    ``content_dir`` is a hole the caller can actually fill.
    """
    if content.system_dir is not None:
        return _SystemRoot(content.system_dir, ROOT_CONTENT_DIRECTORY, sources=(provenance,))
    return _SystemRoot(
        TEMPLATE_CONTENT_DIR, ROOT_CONTENT_DIRECTORY, needs=(HOLE_CONTENT_DIR,), sources=(provenance,)
    )


# What an *absent* ``system_directory`` resolves to, for every route that asks.
# ``config_set_defaults`` seeds it before any config is read
# (``configuration.c:5746-5749``), and on desktop Linux the seed is ``system``
# under the RetroArch config tree (``platform_unix.c:2141-2143``). So the key
# being absent is not a gap: it names a directory, and both the card route and
# the firmware route resolve it to the same one — from here, once.
#
# One qualification, and it is a reachability one: the join is the *else*
# branch. ``LIBRETRO_SYSTEM_DIRECTORY`` in the environment wins when it is set
# (``platform_unix.c:2137-2140``), and atlas cannot read the environment the
# emulator will run with — it is not on disk, and the seam abstracts the
# machine, not the process. [V] unset on the reference machine, so the join is
# what applies there; an installation that exports it is [O] — the answer would
# name this directory while RetroArch used the exported one.
PLATFORM_SYSTEM_DIR_SOURCE = (
    "default: system_directory unset — RetroArch platform default applies "
    "('system' under the config tree, platform_unix.c:2142-2143)"
)


def _platform_system_dir(retroarch_config_dir: str) -> str:
    """RetroArch's own default system directory for this config tree."""
    return os.path.join(retroarch_config_dir, "system")


def _core_system_root(
    *,
    sandbox: _Sandbox,
    cfg_label: str,
    layers: Sequence[_CfgLayer],
    content: _Content,
    retroarch_config_dir: str,
) -> _SystemRoot:
    """Resolve the system directory the way the core receives it (``runloop.c:1958-1999``).

    ``RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY`` is not a read of
    ``system_directory``: the core is handed the **content's** directory
    whenever ``systemfiles_in_content_dir`` is set, and whenever no value is
    left standing at all (``runloop.c:1963-1964``). Only otherwise does it get
    the configured directory (``:1992-1997``).

    What "left standing" means is decided before the environment call, and the
    two spellings that clear it are not the same as the key being absent:

    - **Absent** — ``config_set_defaults`` has already put the platform default
      there (``configuration.c:5746-5749``), which on desktop Linux is
      ``system`` under the config tree (``platform_unix.c:2141-2143``, the same
      block that seeds the saves default this resolver answers with) unless
      ``LIBRETRO_SYSTEM_DIRECTORY`` is exported, which wins (``:2137-2140``) and
      which atlas cannot read off a disk — [V] unset on the reference machine,
      [O] anywhere it is set. So an unset key resolves; it is not a hole and
      never was one.
    - **Blank or the literal ``default``** — ``system_directory`` passes
      ``handle_setting = true`` (``configuration.c:1691``), so the generic path
      loop writes whatever the merged config holds, with no directory test
      (``:6532-6538``); ``config_get_path`` copies an empty value through
      (``config_file.c:1202-1216``) and ``default`` is cleared outright
      (``:6834-6835``). Either way the setting is empty and the content
      directory wins — the opposite of ``savefile_directory``, where a blank
      value keeps the standing root.

    A dropped line is stated here for the first time: while an unset key
    surfaced as a hole, the fact spoke for itself; now it resolves silently to
    the platform default, so the line RetroArch refused has to be said out loud
    (the widened ``cfg-line-dropped`` scope of REVIEW M2/M4).
    """
    in_content_dir, flag_ignored = chain_bool(layers, "systemfiles_in_content_dir", default=False)
    raw_system, dropped = chain_value(layers, "system_directory")
    caveats = list(_ignored_caveats((*flag_ignored, *dropped)))
    configured = sandbox.cfg_path("system_directory", raw_system) if raw_system is not None else None

    if in_content_dir:
        root = _content_system_root(
            content,
            provenance=f'{cfg_label} chain: systemfiles_in_content_dir = "true" — the core is handed the '
            "content's own directory as its system directory (runloop.c:1963-1985)",
        )
    elif raw_system is None:
        root = _SystemRoot(
            _platform_system_dir(retroarch_config_dir),
            ROOT_SYSTEM_DIRECTORY,
            sources=(PLATFORM_SYSTEM_DIR_SOURCE,),
        )
    elif configured is None:
        root = _content_system_root(
            content,
            provenance=f'{cfg_label} chain: system_directory = "{raw_system}" leaves the setting empty — '
            "the core is handed the content's own directory instead (runloop.c:1963-1985)",
        )
    elif configured.path is None:
        # The value as configured: it is where the emulator writes, in the only
        # namespace that names it, and the caveat states that atlas cannot
        # follow it there — the same answer _host_save_dir gives for a saves
        # root it cannot reach.
        root = _SystemRoot(
            raw_system,
            ROOT_SYSTEM_DIRECTORY,
            reachable=False,
            sources=(f'{cfg_label} chain: system_directory = "{raw_system}"',),
            caveats=configured.caveats,
        )
    else:
        root = _SystemRoot(
            configured.path,
            ROOT_SYSTEM_DIRECTORY,
            sources=(f'{cfg_label} chain: system_directory = "{raw_system}"{configured.note}',),
        )
    return _dc_replace(root, caveats=(*caveats, *root.caveats))


def _with_cross_parts(file_set: FileSet, cross_parts: "_CrossParts") -> FileSet:
    """The declared decomposition gains the parts under the other roots.

    The flat ``files`` stays the answer's own directory, as it always was. An
    observed or unknown set keeps its shape — no decomposition exists there to
    extend, and the spans-roots caveats carry the parts instead.
    """
    if cross_parts.groups and file_set.state == FILE_SET_DECLARED and file_set.groups:
        return FileSet(
            state=file_set.state,
            files=file_set.files,
            provenance=file_set.provenance,
            complete=file_set.complete,
            groups=(*file_set.groups, *cross_parts.groups),
        )
    return file_set


@dataclass(frozen=True, slots=True)
class _CrossParts:
    """A mode's resolved cross-root groups, their caveats, and unfilled holes."""

    groups: tuple[FileGroup, ...] = ()
    caveats: tuple[Caveat, ...] = ()
    holes: tuple[str, ...] = ()


def _cross_root_parts(
    mode: SaveMode,
    *,
    card: CoreCard,
    mode_value: str | None,
    sandbox: _Sandbox,
    cfg_label: str,
    layers: Sequence[_CfgLayer],
    content: _Content,
    retroarch_config_dir: str,
) -> _CrossParts:
    """A mode's cross-root groups, resolved: the parts that stay behind elsewhere.

    Flycast's per-game modes move the governed VMU under the save root while
    the console flash — and in 'VMU A1' the three unmoved shared cards — stay
    under the system directory's ``dc``. Each such part resolves against its
    own root the way that root has always been resolved (the system kind can
    legitimately land in the content's directory), and reaches the caller
    twice on purpose: as a :class:`FileGroup` where the set is declared, and
    always as a ``file-set-spans-roots`` caveat naming the directory and the
    files — the carrier that survives an observed answer, exactly the way
    ``file-names-unestablished`` carries MAME's unnamed tree. The third
    element is the holes the bases could not fill, which join the answer's
    ``needs``.
    """
    cross = [group for group in mode.groups if group.root is not None]
    if not cross:
        return _CrossParts()
    bases: dict[str, _SystemRoot] = {}
    for kind in {group.root or "" for group in cross}:
        if kind == ROOT_SYSTEM_DIRECTORY:
            bases[kind] = _core_system_root(
                sandbox=sandbox,
                cfg_label=cfg_label,
                layers=layers,
                content=content,
                retroarch_config_dir=retroarch_config_dir,
            )
        else:
            bases[kind] = _content_system_root(
                content,
                provenance=f"rule card '{card.key}': a cross-root part lies in the content's own tree",
            )
    groups_out: list[FileGroup] = []
    caveats: list[Caveat] = []
    holes: list[str] = []
    suffix = f" in mode {mode_value!r}" if mode_value else ""
    for group in cross:
        base = bases[group.root or ""]
        directory = os.path.join(base.base, *[seg for seg in (group.subdir or "").split("/") if seg])
        filled = _card_files(group.files or (), content.rom_stem)
        names = filled if filled is not None else group.files or ()
        groups_out.append(
            FileGroup(dir=directory, files=names, granularity=group.granularity, role=group.role)
        )
        holes.extend(base.needs)
        caveats.append(
            Caveat(
                CAVEAT_FILE_SET_SPANS_ROOTS,
                f"core {card.key!r}{suffix}: part of the save stays under {directory} — "
                f"{', '.join(names)} — beyond this answer's own root; where the file set is "
                "declared, the same part is in file_set.groups",
                {
                    "core": card.key,
                    "mode": mode_value or "",
                    "dir": directory,
                    "files": ", ".join(names),
                },
            )
        )
    return _CrossParts(tuple(groups_out), tuple(caveats), tuple(dict.fromkeys(holes)))


def _stated_mode_caveats(
    card: CoreCard, mode: SaveMode, granularity: Granularity | None
) -> tuple[Caveat, ...]:
    """The groups-less forms' caveats, wherever the mode's root routed the answer.

    Both statements produce a declared-empty file set that is true as stated,
    and the caveat is what keeps each from reading as "this game has no
    save": inside-content says the loaded content file takes the writes (the
    caller decides what to make of that), writes-discarded says nothing keeps
    any save at all — the granularity block beside it names the switch that
    would change that.
    """
    mode_value = granularity.mode if granularity is not None else None
    suffix = f" in mode {mode_value!r}" if mode_value else ""
    caveats: list[Caveat] = []
    if mode.inside_content is not None:
        caveats.append(
            Caveat(
                CAVEAT_SAVE_INSIDE_CONTENT,
                f"core {card.key!r}{suffix}: no separate save file exists — {mode.inside_content}",
                {"core": card.key, "mode": mode_value or ""},
            )
        )
    if mode.writes_discarded is not None:
        caveats.append(
            Caveat(
                CAVEAT_SAVE_WRITES_DISCARDED,
                f"core {card.key!r}{suffix}: no save exists anywhere — {mode.writes_discarded}",
                {"core": card.key, "mode": mode_value or ""},
            )
        )
    return tuple(caveats)


def _card_root_placement(
    machine: Machine,
    *,
    root: _SystemRoot,
    root_sentence: str,
    card: CoreCard,
    mode: SaveMode,
    granularity: Granularity | None,
    content: _Content,
    sources: tuple[str, ...],
    caveats: tuple[Caveat, ...],
    excluded: frozenset[str] = frozenset(),
) -> SavefilePlacement:
    """The placement for a card mode rooted somewhere the standard rule is not.

    Shared by the system-directory and content-directory routes, because what
    differs between them is only which root was resolved and how the answer
    says so — everything below the root (the subdir join, the caveats, the
    look at the directory, the link view) is the same question.
    """
    directory = os.path.join(root.base, mode.subdir) if mode.subdir else root.base
    card_sources = [
        *sources,
        *root.sources,
        f"rule card '{card.key}': {root_sentence} — {card.provenance}",
    ]
    all_caveats = [*caveats, *root.caveats]
    all_caveats.extend(_stated_mode_caveats(card, mode, granularity))
    if granularity is not None:
        all_caveats.extend(
            _file_set_caveats(
                card,
                mode,
                mode_value=granularity.mode or "",
                rom_stem=content.rom_stem,
            )
        )
        all_caveats.extend(
            _unnamed_tree_caveats(
                card,
                mode,
                directory=directory,
                rom_stem=content.rom_stem,
                content_dir_name=content.dir_name,
            )
        )
    observable = root.reachable and not root.needs
    file_set = _card_file_set(
        machine,
        card=card,
        mode=mode,
        directory=directory,
        rom_stem=content.rom_stem,
        content_dir_name=content.dir_name,
        observable=observable,
        excluded=excluded,
    )
    physical_dir = None
    if observable:
        physical_dir, link_caveats = _link_view(machine, directory)
        all_caveats.extend(link_caveats)
    return SavefilePlacement(
        dir=directory,
        root_kind=root.root_kind,
        needs=needs_with_file_set(root.needs, file_set.files),
        file_set=file_set,
        sources=tuple(card_sources),
        caveats=tuple(all_caveats),
        granularity=granularity,
        physical_dir=physical_dir,
    )


def _system_directory_placement(
    machine: Machine,
    *,
    sandbox: _Sandbox,
    cfg_label: str,
    card: CoreCard,
    mode: SaveMode,
    granularity: Granularity | None,
    layers: Sequence[_CfgLayer],
    content: _Content,
    retroarch_config_dir: str,
    sources: tuple[str, ...],
    caveats: tuple[Caveat, ...],
    excluded: frozenset[str] = frozenset(),
) -> SavefilePlacement:
    """The placement for a card whose core roots its saves in the system directory.

    The card states which directory the core *asks* for; which directory that
    is on this machine is RetroArch's answer, not the cfg key's value
    (:func:`_core_system_root`) — so this route's ``root_kind`` follows the root
    the core is actually handed.
    """
    root = _core_system_root(
        sandbox=sandbox,
        cfg_label=cfg_label,
        layers=layers,
        content=content,
        retroarch_config_dir=retroarch_config_dir,
    )
    return _card_root_placement(
        machine,
        root=root,
        root_sentence=(
            "core keeps saves under the directory RetroArch hands it as the system directory"
        ),
        card=card,
        mode=mode,
        granularity=granularity,
        content=content,
        sources=sources,
        caveats=caveats,
        excluded=excluded,
    )


def _content_directory_placement(
    machine: Machine,
    *,
    sandbox: _Sandbox,
    cfg_label: str,
    card: CoreCard,
    mode: SaveMode,
    granularity: Granularity | None,
    layers: Sequence[_CfgLayer],
    content: _Content,
    retroarch_config_dir: str,
    sources: tuple[str, ...],
    caveats: tuple[Caveat, ...],
    excluded: frozenset[str] = frozenset(),
) -> SavefilePlacement:
    """The placement for a card whose core writes into the content's own tree.

    boom3 hands its engine the ROM tree as the save path, and vitaquake3's
    filesystem falls back to it — neither ever reads the directory it builds
    under the frontend's save root. The root is the content's own directory,
    resolved the way the content-dir cases have always been: concrete when the
    caller named content, the ``<content_dir>`` template with its hole when
    not (:func:`_content_system_root`).
    """
    root = _content_system_root(
        content,
        provenance=(
            f"rule card '{card.key}': the core's writes land in the content's own directory"
        ),
    )
    return _card_root_placement(
        machine,
        root=root,
        root_sentence="core writes into the content's own tree",
        card=card,
        mode=mode,
        granularity=granularity,
        content=content,
        sources=sources,
        caveats=caveats,
        excluded=excluded,
    )


def _sorted_dir_fallback(
    machine: Machine, *, intended_dir: str, effective_root: str | None
) -> tuple[str, str | None, tuple[Caveat, ...]]:
    """Resolve a sorted directory that may not exist — a CONDITIONAL result.

    RetroArch creates it on first save and silently reverts to the unsorted
    root when creation fails (runloop.c:8844). A file in the way makes the
    failure certain — then the fallback IS the answer; anything else keeps the
    intended dir with a structural fallback (REVIEW H5).

    Returns ``(dir, fallback_dir, caveats)``.
    """
    if effective_root is None or intended_dir == effective_root:
        return intended_dir, None, ()
    dir_kind = machine.path_kind(intended_dir)
    if dir_kind == KIND_FILE:
        return (
            effective_root,
            None,
            (
                Caveat(
                    CAVEAT_SORTED_DIR_UNCREATABLE,
                    f"sorted directory {intended_dir} is blocked by an existing file — RetroArch "
                    f"cannot create it and reverts to {effective_root} (runloop.c:8844)",
                    {"intended": intended_dir, "effective": effective_root},
                ),
            ),
        )
    if dir_kind != KIND_DIRECTORY:
        return (
            intended_dir,
            effective_root,
            (
                Caveat(
                    CAVEAT_SORTED_DIR_MISSING,
                    f"sorted directory {intended_dir} does not exist yet — RetroArch creates it on first save, "
                    f"and silently reverts to {effective_root} if creation fails (runloop.c:8844)",
                    {"dir": intended_dir, "fallback_dir": effective_root},
                ),
            ),
        )
    return intended_dir, None, ()


def _observed_file_set(
    machine: Machine,
    *,
    directory: str,
    rom_stem: str,
    content_dir_name: str | None,
    content_path: str | None,
    card: CoreCard | None,
    mode: SaveMode | None,
) -> tuple[FileSet, tuple[Caveat, ...]]:
    """The save files actually lying in *directory* for this ROM, or what is declared.

    Literal observation: ROM names routinely carry glob metacharacters
    (``[``, ``]``) — escaped so ``[`` matches ``[`` (REVIEW M2). RetroArch's own
    bookkeeping next to saves is filtered with a source citation: the
    disk-control index ``<stem>.ldci`` (disk_index_file.c:201-249,
    file_path_special.h:83) is not save data. The content file itself is
    filtered by the name it has on disk — for content inside an archive that is
    the archive, which shares the stem whenever it is named after its entry.

    A directory that could not be listed is the case this exists to keep out of
    the answer: the save data may be sitting right there, on a card that
    stopped answering mid-session. The set is *unknown* then, with a caveat
    naming the directory — never "no files present at …", which is the same
    sentence a genuinely empty directory earns and would send a caller off to
    restore a save it already has.
    """
    if mode is not None and mode.stated is not None:
        # A groups-less statement leaves nothing to observe: the mode says no
        # separate save file exists, and a leftover from an earlier mode lying
        # in the directory must not be presented as this configuration's save.
        return (
            _file_set_of(
                [],
                directory=directory,
                rom_stem=rom_stem,
                content_dir_name=content_dir_name,
                card=card,
                mode=mode,
            ),
            (),
        )
    content_file = content_file_name(content_path) if content_path else None
    pattern = os.path.join(_glob_escape(directory), _glob_escape(rom_stem) + ".*")
    companions = {f"{rom_stem}.ldci"}
    listing = machine.glob(pattern)
    matches = [
        m
        for m in listing.matches
        # In content-dir mode the ROM shares the save's directory and
        # stem — the content file itself is never part of the save set.
        if os.path.basename(m) != content_file and os.path.basename(m) not in companions
    ]
    caveats = tuple(_unlistable_caveat(path) for path in listing.unreadable)
    if listing.unreadable:
        # Not "unless something matched": this pattern names one directory, so
        # a failed listing means zero matches anyway — and if that ever stopped
        # holding, the matches would be a partly-read directory presented as an
        # observed set, which a `complete` rule card could then close over.
        return UNKNOWN_FILE_SET, caveats
    return (
        _file_set_of(
            matches,
            directory=directory,
            rom_stem=rom_stem,
            content_dir_name=content_dir_name,
            card=card,
            mode=mode,
        ),
        caveats,
    )


def _unlistable_caveat(path: str) -> Caveat:
    return Caveat(
        CAVEAT_SAVE_DIR_UNLISTABLE,
        f"{path} could not be listed (permissions or an I/O failure), so whether this content has "
        "saves there is unknown — an empty file set would be a claim about a directory atlas never read",
        {"path": path},
    )


def _fill_subdir(
    subdir: str, *, rom_stem: str | None, content_dir_name: str | None
) -> tuple[str, tuple[str, ...]]:
    """A card subdir with its template segments filled from the content at hand.

    Returns the filled subdir and the holes the content could not fill. A
    content-less question keeps the token in the path and reports its hole —
    the same shape the sorted root's ``<content_dir>`` template has always had,
    so a caller that fills holes already knows what to do with these. The
    loader guarantees a template is a whole segment, which is what keeps
    :func:`_base_of`'s segment counting exact over the filled result.
    """
    values = {TEMPLATE_ROM_STEM: rom_stem, TEMPLATE_CONTENT_DIR_NAME: content_dir_name}
    segments: list[str] = []
    holes: list[str] = []
    for segment in subdir.split("/"):
        value = values.get(segment)
        if segment in values and value is None:
            holes.append(SUBDIR_TEMPLATE_HOLES[segment])
        segments.append(value if value is not None else segment)
    return "/".join(segments), tuple(dict.fromkeys(holes))


def _base_of(directory: str, subdir: str | None) -> str:
    """The root a mode's directory was built from, by undoing its own subdir.

    The subdir is joined on segment by segment (:func:`_nest_card_subdir`), so
    dropping that many components is exact — no string matching against a path
    the machine may spell differently.
    """
    if not subdir:
        return directory
    for _ in [segment for segment in subdir.split("/") if segment]:
        directory = os.path.dirname(directory)
    return directory


def _declared_groups(
    mode: SaveMode | None, *, directory: str, rom_stem: str, content_dir_name: str | None
) -> tuple[FileGroup, ...]:
    """The card's own decomposition of a declared set, each part in its directory.

    **Every group the card knows about**, including the ones whose file names
    follow from nothing atlas reads — those carry ``files=None``. That is what
    makes one walk over ``groups`` reach every place a save lives, rather than a
    walk plus a scan of the caveats for the directories the walk left out. The
    caveat still travels, because it carries the citation and the sentence a
    person reads; it is no longer the only carrier.

    A mode that states no file list of its own is not decomposed at all: there
    is no first group for the flat set to agree with, and a decomposition whose
    parts nobody can order is not one.
    """
    if mode is None or mode.files is None:
        return ()
    base = _base_of(directory, mode.subdir)
    groups: list[FileGroup] = []
    for group in mode.groups:
        if group.root is not None:
            # A cross-root part resolves against its own root, not against
            # this directory's base — _cross_root_parts builds it, and the
            # standard route appends it after this decomposition.
            continue
        names = _card_files(group.files, rom_stem) if group.files is not None else None
        if group.unnamed is None and not names:
            return ()
        subdir, _ = _fill_subdir(
            group.subdir or "", rom_stem=rom_stem or None, content_dir_name=content_dir_name
        )
        groups.append(
            FileGroup(
                dir=os.path.join(base, *[s for s in subdir.split("/") if s]),
                files=names,
                granularity=group.granularity,
                role=group.role,
            )
        )
    return tuple(groups)


def _file_set_of(
    matches: list[str],
    *,
    directory: str,
    rom_stem: str,
    content_dir_name: str | None,
    card: CoreCard | None,
    mode: SaveMode | None,
) -> FileSet:
    """What was found, else what the card declares, else nothing stated."""
    declared = None
    if card is not None and mode is not None and mode.files is not None:
        declared = _card_files(mode.files, rom_stem)
    if matches:
        observed = tuple(sorted(os.path.basename(m) for m in matches))
        complete = (
            mode is not None and mode.complete and declared is not None and set(observed) <= set(declared)
        )
        return FileSet(
            state=FILE_SET_OBSERVED,
            files=observed,
            provenance=f"observed on the machine: {directory}",
            complete=complete,
        )
    if declared is not None and card is not None:
        return FileSet(
            state=FILE_SET_DECLARED,
            files=declared,
            provenance=f"declared by rule card '{card.key}' (none present yet)",
            complete=mode.complete if mode is not None else False,
            groups=_declared_groups(
                mode, directory=directory, rom_stem=rom_stem, content_dir_name=content_dir_name
            ),
        )
    return FileSet(
        state=FILE_SET_UNKNOWN,
        files=(),
        provenance=f"no files present at {directory} — file set not stated (never guessed)",
    )


def _nest_card_subdir(
    directory: str,
    fallback_dir: str | None,
    *,
    card: CoreCard | None,
    mode: SaveMode | None,
    rom_stem: str | None,
    content_dir_name: str | None,
) -> tuple[str, str | None, tuple[str, ...], tuple[str, ...]]:
    """Nest a card core's own subtree under the effective save directory.

    GET_SAVE_DIRECTORY hands the core the redirected (sorted,
    fallback-resolved) dir (runloop.c:2001, set at runloop.c:8977), and the
    core appends its subdir to whatever it received — so the subdir follows the
    fallback too. A mode that nests nothing leaves both paths alone. A subdir
    may template a segment on the content (prboom keys the directory by the
    content's stem, the vitaquake2 family by its directory's basename); the
    filled spelling is what lands in the answer, and what the content could
    not fill stays a token in the path with its hole in the fourth element.
    """
    if mode is None or not mode.subdir or mode.root != ROOT_SAVEFILE_DIRECTORY:
        # Only a savefile-rooted mode's subdir belongs on this route's
        # directory — the other roots resolve on their own routes, and nesting
        # their subdir onto the save directory would state a path nobody uses.
        return directory, fallback_dir, (), ()
    subdir, holes = _fill_subdir(mode.subdir, rom_stem=rom_stem, content_dir_name=content_dir_name)
    sources: tuple[str, ...] = ()
    if card is not None:
        sources = (
            f"rule card '{card.key}': core nests its saves under '{subdir}/' in the save directory",
        )
    nested_fallback = os.path.join(fallback_dir, subdir) if fallback_dir is not None else None
    return os.path.join(directory, subdir), nested_fallback, sources, holes


def _content_dir_caveat(directory: str) -> Caveat:
    """What an observation in the content's own directory can and cannot say.

    The observation is a glob for the name RetroArch saves under
    (runloop.c:8720), and in the ROM's own directory that name belongs to the
    *content* first: the remaining tracks of a ``.cue``, the box art and manual
    a frontend stores beside it, the archive the ROM was extracted from. Telling
    those from save data would take a source that says which extensions are
    content — atlas has none (rule cards state files per core, not per content
    format), and an invented list of ROM extensions is exactly the guess this
    resolver refuses. So the observation is stated for what it is: everything
    lying there under the content's name, never a completeness claim
    (REVIEW M10).
    """
    return Caveat(
        CAVEAT_CONTENT_DIR_OBSERVATION,
        f"the files observed lie in the content's own directory ({directory}) and are matched by the "
        "content's name — files belonging to the content itself (further disc tracks, cover art, "
        "manuals) cannot be told from save data here, so this set may be wider than the save",
        {"dir": directory},
    )


def _is_content_dir(directory: str, content: _Content) -> bool:
    """Is the directory just observed the content's own — the one atlas does not own?

    Normalized on both sides: the same directory can reach the placement as the
    content's own path and as a configured ``savefile_directory`` spelled with a
    trailing slash, and it is the same directory either way.
    """
    if content.dir_path is None:
        return False
    return os.path.normpath(directory) == os.path.normpath(content.dir_path)


@dataclass(frozen=True, slots=True)
class _Declaration:
    """What the standard rule states about a core's files, or that it states nothing.

    One type for every outcome rather than a tuple whose members change shape
    between returns: the empty declaration, the declared set of no files and
    the named set are three different answers, and a caller that unpacks
    positions has to remember which position means what in which case.
    """

    file_set: FileSet | None = None
    granularity: Granularity | None = None
    caveats: tuple[Caveat, ...] = ()


def _drift_caveat(
    record: SaveMemoryRecord, entry: SystemMemory, *, system: str | None, core_version: str | None
) -> Caveat | None:
    """The tripwire both declaration shapes share: a build nobody read this against.

    One caveat or none, rather than a tuple of nought or one: the caller splices
    it into a list either way, and a return whose *length* carries the meaning
    makes every call site read the shape before it reads the fact.
    """
    if core_version is None or entry.verified_core == core_version:
        return None
    return Caveat(
        CAVEAT_UNVERIFIED_VERSION,
        f"which save files {record.key!r} writes for {system} was read from core "
        f"{entry.verified_core}, and this machine runs {core_version} — a build is exactly "
        "what could add or drop a memory id, so the claim is not carried across the "
        "difference unexamined",
        {
            "core": record.key,
            "verification": "drifted",
            "core_verified": entry.verified_core,
            "core_live": core_version,
        },
    )


def _as_tuple(caveat: Caveat | None) -> tuple[Caveat, ...]:
    """One caveat or none, as the tuple an answer's caveat list is spliced from."""
    return (caveat,) if caveat is not None else ()



def _every_recorded_system(record: SaveMemoryRecord) -> str:
    """How an answer names the systems it is about when the caller named none."""
    return ", ".join(sorted(record.systems))


def _across_systems_caveat(record: SaveMemoryRecord, *, system: str | None) -> Caveat | None:
    """Said whenever a record answered without a system to key it by.

    The answer is not weaker for it — every system this record covers writes
    the same files, which is why it could be given at all — but it is *scoped*,
    and the scope is the record's own systems. A core run for a system nobody
    recorded has established nothing here, and a client reading the names as
    universal would carry that mistake into a system atlas never spoke about.
    """
    if system is not None:
        return None
    systems = sorted(record.systems)
    return Caveat(
        CAVEAT_FILE_SET_ACROSS_SYSTEMS,
        f"no system was named, and core {record.key!r} writes the same files for every system it "
        f"is recorded for ({', '.join(systems)}) — so this answer holds for whichever of them the "
        "content is, and states nothing about any other",
        {"core": record.key, "systems": ", ".join(systems)},
    )


def _no_frontend_files(
    record: SaveMemoryRecord,
    entry: SystemMemory,
    *,
    system: str | None,
    core_version: str | None,
    extra: tuple[Caveat, ...] = (),
) -> _Declaration:
    """The established emptiness: RetroArch writes this core no save file at all.

    A declared set of *no* files, which is a different answer from the unknown
    an unrecorded core gets — one says atlas looked and there are none, the
    other says atlas has not looked. Both spell ``files`` empty, and
    ``file_set.state`` is the field that tells them apart, exactly as the
    grammar was built to.

    No granularity travels with it. Granularity says how a save is grouped, and
    there is no save here to group; a value would be a field invented for an
    empty answer.

    The caveat is not decoration. Read alone, "no files" would tell a client
    syncing saves that a Nintendo DS game has nothing to back up — DeSmuME
    fills no libretro memory id (``libretro.cpp:2481`` at desmume 7f05a8d) and
    still keeps its saves somewhere. So the answer states which half is
    established and which is open.
    """
    return _Declaration(
        file_set=FileSet(
            state=FILE_SET_DECLARED,
            files=(),
            provenance=(
                f"declared by the standard rule: core '{record.key}' on {system} fills none of the "
                f"memory ids RetroArch writes files for, so the frontend writes no save file at "
                f"all — {entry.citation}"
            ),
        ),
        caveats=(
            *extra,
            Caveat(
                CAVEAT_CORE_OWN_WRITES_UNESTABLISHED,
                f"RetroArch writes no save file for core {record.key!r} on {system}, which is "
                "established; whether the core writes save files of its own, past the frontend, "
                "is not — an empty file set here is a statement about the frontend, never a "
                "claim that this content has no save",
                {"core": record.key},
            ),
            *_as_tuple(_drift_caveat(record, entry, system=system, core_version=core_version)),
        ),
    )


def _standard_declaration(
    query: _SaveQuery,
    *,
    content: _Content,
    library_name: str | None,
    card: CoreCard | None,
    core_info: CoreInfo | None,
) -> _Declaration:
    """The files RetroArch itself writes for this core on this system, or nothing stated.

    The one file set in this resolver that is *not* a look at the directory,
    and deliberately so: what a save is called follows from RetroArch's naming
    rule and the core's own memory ids, both of which hold whether or not a
    file has been written yet. A file lying in the directory is evidence about
    the past — it may be what a core option wrote before it was switched, or
    what a different core left behind — so it cannot carry a present-tense
    claim about where this configuration writes, and it is not consulted here.

    Five things make the declaration stay silent instead, each of them a
    refusal rather than a fallback:

    - **A rule card speaks for this core.** The card is the stronger statement
      — it knows the deviation this record does not model — and two
      declarations of one file set would be a contradiction a client cannot
      resolve. Any card at all silences the record, not merely one whose mode
      declares files: a carded core is a *deviating* core, and a record that
      filled in the file names of a save the card has moved elsewhere would be
      right about the names and wrong about the save.
    - **No content was named**, so the template's ``<rom_stem>`` has nothing to
      fill it and the shape is not the answer.
    - **The core has no record**, which is not "it writes nothing" but "nobody
      has read its source yet".
    - **The system is unknown or not in the record.** A record is keyed by core
      *and* system because one core is not one behaviour, and narrowing by
      guessing which system was meant is the guess this package refuses.
    - **The installed core could not be examined at all.** A record is read
      out of one build's source, and applying it on the strength of a ``.so``
      file name would state a source-verified claim about a binary nobody
      read. That is the decision :func:`_select_card` already makes for a rule
      card in the same state, and for the same reason: the drift tripwire
      below compares against a version, and *no version at all* is not a
      version that matched.

    The granularity comes out of the same record and is not a second claim:
    the standard rule keys every file it writes by the content, so the only
    question left is how many of them there are, and the record answers it.
    ``option_key`` is ``None`` for the reason LRPS2's is — no option governs
    this, so there is nothing for a caller to switch.
    """
    if card is not None:
        return _Declaration()
    if content.rom_stem is None:
        return _Declaration()
    so_basename = os.path.basename(query.core_so) if query.core_so is not None else None
    record = lookup_save_memory(so_basename=so_basename, library_name=library_name)
    if record is None:
        return _Declaration()
    entry = record.for_system(query.system)
    if entry is None:
        return _Declaration()
    # Named or not, the answer has to say which systems it is about. With one
    # named it is that one; without, the record answered because every system
    # it covers agrees, and the scope is all of them.
    scope = query.system if query.system is not None else _every_recorded_system(record)
    across = _as_tuple(_across_systems_caveat(record, system=query.system))
    # Last, and deliberately after the system check: this is the only refusal
    # here that states a caveat, so it must fire exactly where a declaration
    # was actually withheld. Asked without a system the record could never
    # have spoken anyway, and a caveat there would report the loss of
    # something that was never on offer.
    if core_info is None:
        return _Declaration(
            caveats=(
                Caveat(
                    CAVEAT_CORE_GENERATION_UNESTABLISHED,
                    f"which save files core {record.key!r} writes for {scope} is recorded, "
                    "but the installed core could not be read — which build is here was never "
                    "established, so the recorded file set is not applied and the answer names "
                    "no files",
                    {"core": record.key},
                ),
            ),
        )
    core_version = core_info.library_version
    files = _card_files(entry.file_templates, content.rom_stem)
    if files is None:
        return _Declaration()
    if entry.frontend_writes_nothing:
        return _no_frontend_files(
            record, entry, system=scope, core_version=core_version, extra=across
        )
    return _Declaration(
        file_set=FileSet(
            state=FILE_SET_DECLARED,
            files=files,
            provenance=(
                f"declared by the standard rule: RetroArch names the save after the content "
                f"(runloop.c:8720-8723) and writes only .srm and .rtc (save.c:710-724), and core "
                f"'{record.key}' on {scope} fills {', '.join(entry.memory_types)} — "
                f"{entry.citation}"
            ),
        ),
        granularity=Granularity(
            value=(
                GRANULARITY_PER_GAME_FILE if len(files) == 1 else GRANULARITY_PER_GAME_FILES
            ),
            mode=None,
            readings=(),
            alternatives=(),
            provenance=(
                f"RetroArch's standard rule keys every save file by the content, and '{record.key}' "
                f"on {scope} fills {len(files)} of them — no core option governs this"
            ),
        ),
        caveats=across
        + _as_tuple(_drift_caveat(record, entry, system=scope, core_version=core_version)),
    )


def _observed_at(
    machine: Machine,
    *,
    directory: str,
    content: _Content,
    content_path: str | None,
    card: CoreCard | None,
    mode: SaveMode | None,
    declared: FileSet | None = None,
) -> tuple[FileSet, str | None, tuple[Caveat, ...]]:
    """What lies at the resolved directory: the file set and the link view.

    *declared* short-circuits the file-set half: where the standard rule
    already states which files this core writes, the directory is not listed
    for them at all. That is not an optimisation — a listing would produce
    findings this answer must not state, and the caveat about an unlistable
    directory would report a degradation of something no longer being asked.
    """
    file_set = declared if declared is not None else UNKNOWN_FILE_SET
    caveats: tuple[Caveat, ...] = ()
    if declared is None and content.rom_stem is not None:
        file_set, caveats = _observed_file_set(
            machine,
            directory=directory,
            rom_stem=content.rom_stem,
            content_dir_name=content.dir_name,
            content_path=content_path,
            card=card,
            mode=mode,
        )
        if file_set.state == "observed" and _is_content_dir(directory, content):
            caveats = (*caveats, _content_dir_caveat(directory))
    physical_dir, link_caveats = _link_view(machine, directory)
    return file_set, physical_dir, (*caveats, *link_caveats)


def _against_the_filesystem(
    machine: Machine,
    *,
    intended_dir: str,
    root_kind: RootKind,
    layout: RetroArchCfg,
    platform_default_dir: str,
    content: _Content,
    content_path: str | None,
    card: CoreCard | None,
    mode: SaveMode | None,
    declared: FileSet | None,
    reachable: bool,
) -> tuple[str, str | None, str | None, FileSet | None, tuple[str, ...], tuple[str, ...], tuple[Caveat, ...]]:
    """What the machine does to a resolved directory: fallback, nesting, and the look.

    Split out of :func:`_standard_placement` because it is the one part of that
    answer the filesystem gets a say in, and *reachable* gates it twice: a
    sandbox path with no host location still resolves to a directory the
    emulator writes to, but every read of that directory would be a read that
    never applied. The card's own subtree is nested either way — it follows
    from the card, not from the disk.

    Returns ``None`` for the file set where nothing was looked at, so the
    caller keeps whatever it already stated rather than having an unknown
    written over a declaration.

    A card subdir the content could not fill gates the look the same way
    *reachable* does: the path still carries a template token, so there is no
    directory to read — the holes travel back for ``needs`` instead.
    """
    fallback_dir: str | None = None
    physical_dir: str | None = None
    file_set: FileSet | None = None
    caveats: list[Caveat] = []
    final_dir = intended_dir
    if reachable:
        effective_root = (
            content.dir_path
            if root_kind == ROOT_CONTENT_DIRECTORY
            else layout.directory or platform_default_dir
        )
        final_dir, fallback_dir, sorted_dir_caveats = _sorted_dir_fallback(
            machine, intended_dir=final_dir, effective_root=effective_root
        )
        caveats.extend(sorted_dir_caveats)
    final_dir, fallback_dir, subdir_sources, subdir_holes = _nest_card_subdir(
        final_dir,
        fallback_dir,
        card=card,
        mode=mode,
        rom_stem=content.rom_stem,
        content_dir_name=content.dir_name,
    )
    if reachable and not subdir_holes:
        file_set, physical_dir, link_caveats = _observed_at(
            machine,
            directory=final_dir,
            content=content,
            content_path=content_path,
            card=card,
            mode=mode,
            declared=declared,
        )
        caveats.extend(link_caveats)
    return final_dir, fallback_dir, physical_dir, file_set, subdir_sources, subdir_holes, tuple(caveats)


def _standard_placement(
    machine: Machine,
    query: _SaveQuery,
    *,
    layout: RetroArchCfg,
    platform_default_dir: str,
    content: _Content,
    core: _CoreIdentity,
    card: CoreCard | None,
    mode: SaveMode | None,
    granularity: Granularity | None,
    cross_parts: _CrossParts,
    reachable: bool,
    sources: tuple[str, ...],
    caveats: tuple[Caveat, ...],
) -> SavefilePlacement:
    """The standard rule: RetroArch's own path math, then what lies at the result.

    Whatever the configs resolved to is only the intended directory — the
    filesystem still decides, so a sorted directory that is not there yet, a
    card core's nested subtree and the files actually present are all resolved
    against the machine before the answer is stated.

    Unless the root cannot be reached from here (*reachable* false, a sandbox
    path with no host location): the path math still applies and ``dir`` still
    names where the emulator writes, but every observation of the result —
    whether the sorted directory exists, what it links to, which files lie in
    it — would come from a read that never applied, so none is performed and
    ``fallback_dir`` stays empty rather than claiming a fallback nobody takes.
    """
    all_caveats = list(caveats)
    if card is not None and mode is not None:
        all_caveats.extend(_stated_mode_caveats(card, mode, granularity))
    if card is not None and mode is not None and granularity is not None:
        all_caveats.extend(
            _file_set_caveats(
                card,
                mode,
                mode_value=granularity.mode or "",
                rom_stem=content.rom_stem,
            )
        )

    declaration = _standard_declaration(
        query,
        content=content,
        library_name=core.library_name,
        card=card,
        core_info=core.info,
    )
    declared = declaration.file_set
    all_caveats.extend(declaration.caveats)
    # A card's granularity is the stronger word and keeps precedence; the
    # standard rule fills the field only where no card spoke. A declaration
    # holds without reading anything, so it is also the answer where the root
    # cannot be reached from here — what the unreachable branch withholds is
    # every *observation*, and this is not one.
    granularity = granularity or declaration.granularity
    file_set = declared or UNKNOWN_FILE_SET
    placement = build_savefile_placement(
        layout=layout,
        platform_default_dir=platform_default_dir,
        content_dir_path=content.dir_path,
        content_dir_name=content.dir_name,
        library_name=core.library_name,
        extra_sources=sources,
    )

    final_dir = placement.dir
    fallback_dir: str | None = None
    physical_dir: str | None = None
    final_sources = list(placement.sources)
    # Which world knowledge produced this answer — said once per answer, on
    # this route as much as on the system_directory one. It carries what the
    # placement's own fields cannot: that a mode *moves* the save rather than
    # adding to it, so the files the previous mode wrote are left behind stale.
    if card is not None and mode is not None:
        final_sources.append(f"rule card '{card.key}' governs this placement — {card.provenance}")
    subdir_holes: tuple[str, ...] = ()
    if not placement.needs:
        final_dir, fallback_dir, physical_dir, looked_at, subdir_sources, subdir_holes, machine_caveats = (
            _against_the_filesystem(
                machine,
                intended_dir=final_dir,
                root_kind=placement.root_kind,
                layout=layout,
                platform_default_dir=platform_default_dir,
                content=content,
                content_path=query.content_path,
                card=card,
                mode=mode,
                declared=declared,
                reachable=reachable,
            )
        )
        final_sources.extend(subdir_sources)
        all_caveats.extend(machine_caveats)
        file_set = looked_at or file_set
    else:
        # A hole in the base withholds every observation, not the card's own
        # subtree: the core appends its subdir to whatever GET_SAVE_DIRECTORY
        # hands it, sorted or not, so the path math still applies — without it
        # the answer would name the parent as the final directory.
        final_dir, _, subdir_sources, subdir_holes = _nest_card_subdir(
            final_dir,
            None,
            card=card,
            mode=mode,
            rom_stem=content.rom_stem,
            content_dir_name=content.dir_name,
        )
        final_sources.extend(subdir_sources)
    # After the filesystem step, because a card's own subtree is nested onto the
    # directory there and this caveat names directories.
    if card is not None and mode is not None and granularity is not None:
        all_caveats.extend(
            _unnamed_tree_caveats(
                card,
                mode,
                directory=final_dir,
                rom_stem=content.rom_stem,
                content_dir_name=content.dir_name,
            )
        )
    all_caveats.extend(cross_parts.caveats)
    file_set = _with_cross_parts(file_set, cross_parts)

    return SavefilePlacement(
        dir=final_dir,
        root_kind=placement.root_kind,
        needs=needs_with_file_set(
            [*placement.needs, *subdir_holes, *cross_parts.holes], file_set.files
        ),
        file_set=file_set,
        sources=tuple(final_sources),
        caveats=tuple(all_caveats),
        granularity=granularity,
        fallback_dir=fallback_dir,
        physical_dir=physical_dir,
    )


@dataclass(frozen=True, slots=True)
class _Chain:
    """One family's governing sources, read once and resolved — what both routes share.

    Everything up to the point where the two questions part company: the
    content's coordinates, what the core said about itself, the override gates
    and the layers they admitted, the resolved layout, and the platform default
    its root falls back to. Frozen tuples rather than the caller's own lists,
    because a route that appended to a shared list would be editing the other
    route's evidence.
    """

    content: _Content
    core: _CoreIdentity
    gates: _OverrideGates
    layers: tuple[_CfgLayer, ...]
    layout: RetroArchCfg
    # The global cfg's settings from the one parse this chain made. Carried
    # rather than re-derived: the snapshot is the same either way, but a route
    # that parses the text again pays for a second walk of a file that runs to
    # thousands of lines on a real installation.
    global_values: Mapping[str, str]
    reachable: bool
    platform_default_dir: str
    retroarch_config_dir: str
    sources: tuple[str, ...]
    caveats: tuple[Caveat, ...]

    @property
    def effective_root(self) -> str:
        """The root that stands — the configured one, or the platform default."""
        return self.layout.directory or self.platform_default_dir


# The consequence of naming no core, per family — see NO_CORE_FOR_SAVES.
_NO_CORE_MESSAGES = {
    SAVEFILE_KEYS.label: NO_CORE_FOR_SAVES,
    SAVESTATE_KEYS.label: NO_CORE_FOR_STATES,
}


def _read_chain(machine: Machine, query: _SaveQuery, keys: LayoutKeys) -> _Chain:
    """Global cfg → override chain → resolved layout, for one family, all live.

    Reads the same four layers RetroArch reads (``configuration.c:7095``) and
    resolves ``library_name`` from the core binary when a core is named. Every
    degradation is a stated caveat, never a silent guess. The query carries the
    global cfg's content, read exactly once by the caller — one query derives
    every decision from one snapshot of each source (REVIEW M4).

    *keys* picks the family. Nothing below is written twice for savestates:
    RetroArch reads both quartets in one function and applies the same rule to
    each (``runloop.c:8752-8979``), so the port takes the same shape.
    """
    content = _content_coordinates(query.content_path)
    core = _identify_core(
        machine,
        core_so=query.core_so,
        core_path_resolver=query.core_path_resolver,
        no_core_message=_NO_CORE_MESSAGES[keys.label],
    )
    caveats = [*query.extra_caveats, *core.caveats]
    if query.content_path is not None and content.rom_stem is None:
        caveats.append(_unnamed_content_caveat(query.content_path))
    sources_extra = [*query.extra_sources, *core.sources]

    retroarch_config_dir = os.path.dirname(query.global_cfg_path)
    gates = _override_gates(
        query.global_text,
        sandbox=query.sandbox,
        cfg_label=query.cfg_label,
        override_config_dir=query.override_config_dir,
        config_file_dir=retroarch_config_dir,
    )
    sources_extra.extend(gates.sources)
    caveats.extend(gates.caveats)
    overrides, override_sources = _override_layers(
        machine, gates=gates, library_name=core.library_name, content=content
    )
    sources_extra.extend(override_sources)

    layout = resolve_layout(
        query.global_text,
        keys=keys,
        home=query.sandbox.expansion_home,
        cfg_label=query.cfg_label,
        defaults=query.defaults,
        overrides=overrides,
        is_directory=_save_dir_probe(machine, query.sandbox, keys.directory),
    )
    caveats.extend(_ignored_caveats(layout.ignored))
    layers: list[_CfgLayer] = [
        (label, text)
        for label, text in ((query.cfg_label, query.global_text), *overrides)
        if text is not None
    ]
    saves_root = _host_save_dir(query.sandbox, layout)
    layout = saves_root.layout
    sources_extra.extend(saves_root.sources)
    caveats.extend(saves_root.caveats)

    # The RetroArch platform default root — 'saves' or 'states' under the config
    # tree (platform_unix.c:2133-2136) — is the effective root whenever no layer
    # left a usable value: the key unset everywhere, reset to "default", or every
    # value RetroArch read refused by its own directory test.
    platform_default_dir = os.path.join(retroarch_config_dir, keys.platform_default_subdir)
    caveats.extend(
        _rejected_dir_caveats(
            machine,
            query.sandbox,
            layout.rejected_directories,
            key=keys.directory,
            effective=layout.directory or platform_default_dir,
        )
    )

    return _Chain(
        content=content,
        core=core,
        gates=gates,
        layers=tuple(layers),
        layout=layout,
        global_values=parse_cfg_text(query.global_text) if query.global_text is not None else {},
        reachable=saves_root.reachable,
        platform_default_dir=platform_default_dir,
        retroarch_config_dir=retroarch_config_dir,
        sources=tuple(sources_extra),
        caveats=tuple(caveats),
    )


def _working_directory_placement(
    machine: Machine,
    *,
    sandbox: _Sandbox,
    cfg_label: str,
    card: CoreCard,
    mode: SaveMode,
    granularity: Granularity | None,
    layers: Sequence[_CfgLayer],
    content: _Content,
    retroarch_config_dir: str,
    sources: tuple[str, ...],
    caveats: tuple[Caveat, ...],
    excluded: frozenset[str] = frozenset(),
) -> SavefilePlacement:
    """The placement for a card whose core writes relative to the launch's cwd.

    DeSmuME 2015 composes its save path from a variable its libretro build
    never fills, so the file lands relative to the working directory of
    whatever process loaded the core — a property of the launch, not of the
    machine, so no read here can resolve it. The answer stays a ``<cwd>``
    template with its hole in ``needs``: the caller is often the launcher,
    and the launcher knows its own working directory. The caveat carries the
    sentence, so the template is never mistaken for a directory atlas merely
    failed to fill.

    The unused parameters keep the three diverted routes one signature, which
    is what lets the router pick a route instead of a call shape.
    """
    del sandbox, cfg_label, layers, retroarch_config_dir, excluded
    root = _SystemRoot(
        TEMPLATE_CWD,
        ROOT_WORKING_DIRECTORY,
        needs=(HOLE_CWD,),
        reachable=False,
        sources=(
            f"rule card '{card.key}': the core writes relative to the launching process's "
            "working directory",
        ),
    )
    launch_caveat = Caveat(
        CAVEAT_SAVE_DIR_LAUNCH_DEPENDENT,
        f"core {card.key!r} composes its save path from a location its build never sets, so the "
        "file lands relative to the working directory of whatever process loads the core — a "
        "property of the launch, not of the machine. The file names are stated; fill 'cwd' with "
        "the launcher's working directory to complete the path",
        {"core": card.key},
    )
    return _card_root_placement(
        machine,
        root=root,
        root_sentence="core writes relative to the launching process's working directory",
        card=card,
        mode=mode,
        granularity=granularity,
        content=content,
        sources=sources,
        caveats=(*caveats, launch_caveat),
    )


def _with_revocation(
    outcome: "SavefilePlacement | SavestatePlacement | Unresolved", query: _SaveQuery
) -> "SavefilePlacement | SavestatePlacement | Unresolved":
    """*outcome* with the query's revocation statement, where one holds for its directory.

    The one place the check runs, so every route through the two save
    resolvers — the entry routes included — inherits it. A refusal has no
    directory to check, and a template directory (a ``<cwd>`` root) is not an
    absolute host path, which the check itself declines.
    """
    if query.revocation is None or isinstance(outcome, Unresolved):
        return outcome
    caveat = query.revocation(outcome.dir)
    if caveat is None:
        return outcome
    return _dc_replace(outcome, caveats=(*outcome.caveats, caveat))


def _retroarch_savefile_location(machine: Machine, query: _SaveQuery) -> SavefilePlacement | Unresolved:
    """:func:`_savefile_location_resolved` plus the filesystem-revocation statement (issue #103)."""
    return cast(
        "SavefilePlacement | Unresolved",
        _with_revocation(_savefile_location_resolved(machine, query), query),
    )


def _savefile_location_resolved(machine: Machine, query: _SaveQuery) -> SavefilePlacement | Unresolved:
    """Where the savefile lands: the shared chain, then the per-core rule cards.

    The cards are what this route has and the savestate one does not — cores
    write their own save data and can put it elsewhere entirely, which is a
    thing no core can do to a savestate (:func:`_retroarch_savestate_location`).

    A core the machine established is not installed ends the question instead of
    steering it: there is no answer to give about where a core that is not here
    keeps its saves, and the typed refusal says so.
    """
    chain = _read_chain(machine, query, SAVEFILE_KEYS)
    if chain.core.not_installed is not None:
        return chain.core.not_installed
    content, core = chain.content, chain.core
    layout, layers = chain.layout, list(chain.layers)
    retroarch_config_dir = chain.retroarch_config_dir
    platform_default_dir = chain.platform_default_dir
    sources_extra = list(chain.sources)
    caveats = list(chain.caveats)

    # Rule cards: cores whose save behaviour deviates from the standard rule.
    # The card names the governing option; its current value is read live.
    so_basename = os.path.basename(query.core_so) if query.core_so is not None else None
    choice = _select_card(so_basename=so_basename, core_info=core.info)
    sources_extra.extend(choice.sources)
    caveats.extend(choice.caveats)

    card = choice.card
    card_mode: SaveMode | None = None
    granularity: Granularity | None = None
    if card is not None:
        notes, verification_caveats = _verification_notes(
            card,
            arrangement=query.arrangement,
            arrangement_version=query.arrangement_version,
            core_version=core.info.library_version if core.info is not None else None,
            feature_confirmed=choice.live_option is not None or bool(choice.live_options),
        )
        sources_extra.extend(notes)
        caveats.extend(verification_caveats)
        applied = _apply_card(
            machine,
            sandbox=query.sandbox,
            retroarch_config_dir=retroarch_config_dir,
            cfg_label=query.cfg_label,
            layout=layout,
            card=card,
            live_option=choice.live_option,
            live_options=choice.live_options,
            library_name=core.library_name,
            layers=layers,
            content=content,
            gates=chain.gates,
        )
        card, card_mode, granularity = applied.card, applied.mode, applied.granularity
        caveats.extend(applied.caveats)

    if card is not None and card_mode is not None and card_mode.root != ROOT_SAVEFILE_DIRECTORY:
        diverted = {
            ROOT_SYSTEM_DIRECTORY: _system_directory_placement,
            ROOT_CONTENT_DIRECTORY: _content_directory_placement,
            ROOT_WORKING_DIRECTORY: _working_directory_placement,
        }[card_mode.root]
        return diverted(
            machine,
            sandbox=query.sandbox,
            cfg_label=query.cfg_label,
            card=card,
            mode=card_mode,
            granularity=granularity,
            layers=layers,
            content=content,
            retroarch_config_dir=retroarch_config_dir,
            sources=tuple(sources_extra),
            caveats=tuple(caveats),
            excluded=applied.excluded_observations,
        )

    cross_parts = _CrossParts()
    if card is not None and card_mode is not None:
        cross_parts = _cross_root_parts(
            card_mode,
            card=card,
            mode_value=granularity.mode if granularity is not None else None,
            sandbox=query.sandbox,
            cfg_label=query.cfg_label,
            layers=layers,
            content=content,
            retroarch_config_dir=retroarch_config_dir,
        )
    return _standard_placement(
        machine,
        query,
        layout=layout,
        platform_default_dir=platform_default_dir,
        content=content,
        core=core,
        card=card,
        mode=card_mode,
        granularity=granularity,
        cross_parts=cross_parts,
        reachable=chain.reachable,
        sources=tuple(sources_extra),
        caveats=tuple(caveats),
    )


# What RetroArch names a savestate, and therefore what atlas globs for. The
# base is the content's stem with ".state" appended (runloop.c:8942-8949,
# FILE_PATH_STATE_EXTENSION at file_path_special.h:44); slot N above zero
# appends the number and the auto slot appends ".auto" (runloop.c:8185-8207).
# With savestate_thumbnail_enable a ".png" is written beside each of them
# (task_save.c:1226-1230 -> task_screenshot.c:476-485), which is why the glob
# is a prefix match rather than the three exact names.
STATE_EXTENSION = ".state"


def _observed_state_set(
    machine: Machine, *, directory: str, rom_stem: str
) -> tuple[FileSet, tuple[Caveat, ...]]:
    """The savestates actually lying in *directory* for this ROM.

    The glob is ``<stem>.state*``, and the narrowness is the point. A savefile
    observation has to match ``<stem>.*`` because the extensions are the core's
    own and no source lists them; a savestate is written by RetroArch alone,
    under a name this module can state, so the pattern names exactly the
    artifacts of this question. What that buys is precision in the one place
    the save route has to hedge: the ROM's own directory. There ``<stem>.*``
    sweeps up further disc tracks, cover art and the archive itself and the
    answer says so (``content-dir-observation``), while ``<stem>.state*``
    matches none of them — so no such hedge is needed here.

    Neighbours that share the directory and are *not* savestates stay out by
    construction: the ``.srm`` of a machine whose two roots coincide, the
    ``.replay`` of a recorded input movie (``runloop.c:8923``, ``:8950`` put it
    in this very directory), the ``.cht`` cheat file. A replay is a different
    artifact answering a different question, and folding it in would make a
    state set that no state ever wrote.

    A directory that could not be listed yields *unknown* with a caveat naming
    it — never "no states present", which is the sentence an empty directory
    earns and would send a caller off to restore a state it already has.
    """
    pattern = os.path.join(_glob_escape(directory), _glob_escape(rom_stem) + STATE_EXTENSION + "*")
    listing = machine.glob(pattern)
    caveats = tuple(_unlistable_caveat(path) for path in listing.unreadable)
    if listing.unreadable:
        return UNKNOWN_FILE_SET, caveats
    if not listing.matches:
        return (
            FileSet(
                state=FILE_SET_UNKNOWN,
                files=(),
                provenance=(
                    f"no savestates present at {directory} — file set not stated (never guessed)"
                ),
            ),
            caveats,
        )
    return (
        FileSet(
            state=FILE_SET_OBSERVED,
            files=tuple(sorted(os.path.basename(match) for match in listing.matches)),
            provenance=f"observed on the machine: {directory}",
            # Never closed: which slots exist is a live setting away from
            # changing, and nothing on disk says how many were ever written.
            complete=False,
        ),
        caveats,
    )


def _savestate_support_caveats(
    machine: Machine,
    *,
    sandbox: _Sandbox,
    parsed: Mapping[str, str],
    core_so: str,
    layers: Sequence[_CfgLayer],
) -> tuple[Caveat, ...]:
    """What the core's own ``.info`` says about whether it can make savestates.

    ``savestate = "false"`` sets the support level to
    ``CORE_INFO_SAVESTATE_DISABLED`` (``core_info.c:1841-1860``), and RetroArch
    checks that level before it offers a state at all
    (``core_info.c:2899-2937``). 68 of the 292 ``.info`` files a stock RetroDECK
    ships declare it, so a state placement that said only "here is the
    directory" would be technically true and read as a promise for a quarter of
    the matrix.

    It is stated as a caveat rather than as a refusal because the declaration
    does not bind absolutely, and both escapes are visible from here: the cfg
    key ``core_info_savestate_bypass`` waves the check through entirely
    (``:2904-2905``), and for the BASIC level a running core reporting a nonzero
    ``retro_serialize_size()`` overrides stale metadata (``:2926-2929``) — which
    atlas cannot evaluate, because it is a fact about a running core and not
    about anything on disk. So the message names them and the directory answer
    stands.

    Silence here means the declaration says states work, or the bypass is on.
    A ``.info`` that could not be read is not silence: it goes out as
    ``core-info-unreadable``, the same code the firmware route states for the
    same file, because "atlas could not look" and "the core is fine" are the
    two things this grammar may never collapse.
    """
    bypass, ignored = chain_bool(layers, "core_info_savestate_bypass", default=False)
    caveats = _ignored_caveats(ignored)
    if bypass:
        # The check the declaration feeds is skipped wholesale, so the
        # declaration says nothing about this machine's behaviour.
        return caveats
    info_dir, dir_caveats = _cfg_directory(sandbox, parsed, "libretro_info_path")
    if info_dir is None:
        return (*caveats, *dir_caveats, _info_path_unresolved_caveat())
    info_path = os.path.join(info_dir, os.path.basename(core_so).removesuffix(".so") + ".info")
    read = machine.read_text(info_path)
    if read.text is None:
        return (*caveats, _core_info_unreadable_caveat(core_so, read.status))
    declared = parse_core_info(read.text).get("savestate")
    # Only an explicit, well-formed false lowers the level. An absent key and a
    # spelling config_get_bool refuses (one shipped .info says
    # savestate = "serialized") both leave the block unentered and the
    # DETERMINISTIC default standing (core_info.c:1836-1861).
    if declared is None or cfg_bool(declared) is not False:
        return caveats
    return (
        *caveats,
        Caveat(
            CAVEAT_CORE_SAVESTATES_UNSUPPORTED,
            f"{os.path.basename(core_so)} declares savestate = \"{declared}\" in its .info, so RetroArch "
            "treats it as having no savestate support (core_info.c:1841-1860, checked at :2899-2937) — "
            "the directory below is where a state would go, not a promise that one can be made. Two "
            "things still override the declaration: core_info_savestate_bypass = \"true\" in the cfg, and "
            "a running core reporting a nonzero retro_serialize_size(), which atlas cannot ask from here",
            {"core_so": os.path.basename(core_so), "declared": declared},
        ),
    )


def _info_path_unresolved_caveat() -> Caveat:
    """``libretro_info_path`` names no readable directory, so no ``.info`` was read."""
    return Caveat(
        CAVEAT_INFO_PATH_UNRESOLVED,
        "libretro_info_path does not resolve to a readable directory on this machine — whether this "
        "core declares savestate support could not be established, so the absence of a warning below "
        "is not a statement that states work",
    )


def _core_info_unreadable_caveat(core_so: str, status: ReadStatus) -> Caveat:
    """The core's ``.info`` is there in name only — the same gap the firmware route states."""
    return Caveat(
        CAVEAT_CORE_INFO_UNREADABLE,
        f"{os.path.basename(core_so)}'s .info could not be read ({status}) — whether this core declares "
        "savestate support is unknown, so no warning below is not 'states work'",
        {"core_so": os.path.basename(core_so), "status": status},
    )


def _retroarch_savestate_location(machine: Machine, query: _SaveQuery) -> SavestatePlacement | Unresolved:
    """:func:`_savestate_location_resolved` plus the filesystem-revocation statement (issue #103)."""
    return cast(
        "SavestatePlacement | Unresolved",
        _with_revocation(_savestate_location_resolved(machine, query), query),
    )


def _savestate_location_resolved(machine: Machine, query: _SaveQuery) -> SavestatePlacement | Unresolved:
    """Where the savestate lands: the shared chain, and nothing per-core after it.

    The savefile route continues into rule cards here, because a core writes
    its own save data and may put it anywhere. A savestate has no such branch:
    the libretro API hands a core no savestate directory (``libretro.h``), so
    RetroArch serializes and writes the file itself and the four cfg keys are
    the whole story. What the core does get to say is whether it can be
    serialized at all, which is a caveat and not a placement
    (:func:`_savestate_support_caveats`).

    The core reaches this route through the same lookup the savefile route uses,
    so it refuses identically for a core that is not installed. Sorting by core
    and the savestate-support declaration are both answers about that core, and
    a directory offered for one the machine does not have would be invented.
    """
    chain = _read_chain(machine, query, SAVESTATE_KEYS)
    if chain.core.not_installed is not None:
        return chain.core.not_installed
    content = chain.content
    caveats = list(chain.caveats)

    if query.core_so is not None:
        # Asked of the .info whether or not the binary itself loaded: the two
        # are separate reads of separate files, and a core that would not load
        # is exactly one whose declaration is still worth reporting.
        caveats.extend(
            _savestate_support_caveats(
                machine,
                sandbox=query.sandbox,
                parsed=chain.global_values,
                core_so=query.core_so,
                layers=chain.layers,
            )
        )

    placement = build_savestate_placement(
        layout=chain.layout,
        platform_default_dir=chain.platform_default_dir,
        content_dir_path=content.dir_path,
        content_dir_name=content.dir_name,
        library_name=chain.core.library_name,
        extra_sources=chain.sources,
    )

    final_dir = placement.dir
    fallback_dir: str | None = None
    physical_dir: str | None = None
    file_set = UNKNOWN_FILE_SET
    if not placement.needs and chain.reachable:
        effective_root = (
            content.dir_path
            if placement.root_kind == STATE_ROOT_CONTENT_DIRECTORY
            else chain.effective_root
        )
        final_dir, fallback_dir, sorted_caveats = _sorted_dir_fallback(
            machine, intended_dir=final_dir, effective_root=effective_root
        )
        caveats.extend(sorted_caveats)
        if content.rom_stem is not None:
            file_set, set_caveats = _observed_state_set(
                machine, directory=final_dir, rom_stem=content.rom_stem
            )
            caveats.extend(set_caveats)
        physical_dir, link_caveats = _link_view(machine, final_dir)
        caveats.extend(link_caveats)

    return SavestatePlacement(
        dir=final_dir,
        root_kind=placement.root_kind,
        needs=placement.needs,
        file_set=file_set,
        sources=placement.sources,
        caveats=tuple(caveats),
        fallback_dir=fallback_dir,
        physical_dir=physical_dir,
    )


_SCREENSHOT_DIRECTORY_KEY = "screenshot_directory"


def _screenshot_layers(
    machine: Machine, query: _SaveQuery
) -> tuple[_Content, _CoreIdentity, list[_CfgLayer], list[str], list[Caveat]]:
    """The shared preamble, without a family layout: content, core, cfg layers.

    The screenshot family reads three keys of its own through the same
    override chain the save families read theirs through, but none of the
    save layout applies — no platform default seeds a directory for it, and
    an empty key falls to the content's directory instead — so this takes
    the chain-building half of :func:`_read_chain` and leaves the layout
    resolution to the caller.
    """
    content = _content_coordinates(query.content_path)
    core = _identify_core(
        machine,
        core_so=query.core_so,
        core_path_resolver=query.core_path_resolver,
        no_core_message=NO_CORE_FOR_SCREENSHOTS,
    )
    caveats = [*query.extra_caveats, *core.caveats]
    if query.content_path is not None and content.rom_stem is None:
        caveats.append(_unnamed_content_caveat(query.content_path))
    sources = [*query.extra_sources, *core.sources]
    retroarch_config_dir = os.path.dirname(query.global_cfg_path)
    gates = _override_gates(
        query.global_text,
        sandbox=query.sandbox,
        cfg_label=query.cfg_label,
        override_config_dir=query.override_config_dir,
        config_file_dir=retroarch_config_dir,
    )
    sources.extend(gates.sources)
    caveats.extend(gates.caveats)
    overrides, override_sources = _override_layers(
        machine, gates=gates, library_name=core.library_name, content=content
    )
    sources.extend(override_sources)
    layers: list[_CfgLayer] = [
        (label, text)
        for label, text in ((query.cfg_label, query.global_text), *overrides)
        if text is not None
    ]
    return content, core, layers, sources, caveats


def _screenshot_root(
    machine: Machine,
    query: _SaveQuery,
    layers: list[_CfgLayer],
    sources: list[str],
    caveats: list[Caveat],
) -> tuple[str | None, bool]:
    """The configured screenshot root as this machine holds it, or ``None``.

    ``None`` is every way the key ends up empty: unset, reset to the literal
    ``"default"``, or pointing at something that is not an existing directory
    — RetroArch clears that at config load rather than creating it
    (configuration.c:6733-6741), and the caveat states it machine-readably.
    The second element is false where the spelling exists only inside the
    emulator's sandbox: the value stands, and nothing below it can be looked
    at from here.
    """
    raw, dropped = chain_value(layers, _SCREENSHOT_DIRECTORY_KEY)
    caveats.extend(_ignored_caveats(dropped))
    if raw is None:
        return None, True
    configured = raw.strip()
    if not configured or configured == "default":
        # configuration.c:6735-6736 — the literal "default" is the reset
        # spelling, same as the save families' key.
        sources.append(
            f'{_SCREENSHOT_DIRECTORY_KEY} = "{raw}" — the reset spelling; the key counts as '
            "unset (configuration.c:6735-6736)"
        )
        return None, True
    if configured.startswith("~") and query.sandbox.expansion_home is not None:
        # An unset home leaves the tilde verbatim, exactly as the save
        # families' resolution does.
        configured = query.sandbox.expansion_home + configured[1:]
    resolved = query.sandbox.host(_SCREENSHOT_DIRECTORY_KEY, configured)
    sources.append(f'{query.cfg_label}: {_SCREENSHOT_DIRECTORY_KEY} = "{raw}"{resolved.note}')
    if resolved.path is None:
        caveats.extend(resolved.caveats)
        return configured, False
    if machine.path_kind(resolved.path) != KIND_DIRECTORY:
        looked_at = (
            f" (atlas looked at its host spelling {resolved.path!r})"
            if resolved.path != configured
            else ""
        )
        caveats.append(
            Caveat(
                CAVEAT_INVALID_SCREENSHOT_DIRECTORY,
                f"{_SCREENSHOT_DIRECTORY_KEY} {raw!r} is not an existing directory{looked_at} — "
                "RetroArch clears it at config load rather than creating it, and the shots land "
                "in the content's own directory (configuration.c:6733-6741)",
                {"configured": raw},
            )
        )
        return None, True
    return resolved.path, True


def _retroarch_screenshot_location(
    machine: Machine, query: _SaveQuery
) -> ScreenshotPlacement | Unresolved:
    """Where RetroArch writes this configuration's screenshots (issue #142).

    Three keys through the override chain, and the same refusal as every
    other question for a named core that is not installed. The directory
    math is RetroArch's own (task_screenshot.c:488-556 at a79435a): the
    in-content flag outranks even a configured directory, an empty or
    cleared key falls to the content's directory, and sorting by the
    content's directory name applies only under a configured root. The
    directory is created at the moment of the shot, so no existence claim
    or fallback rides the sorted answer.
    """
    content, core, layers, sources, caveats = _screenshot_layers(machine, query)
    if core.not_installed is not None:
        return core.not_installed
    root, reachable = _screenshot_root(machine, query, layers, sources, caveats)
    in_content, in_ignored = chain_bool(layers, "screenshots_in_content_dir", default=False)
    sort_by_content, sort_ignored = chain_bool(
        layers, "sort_screenshots_by_content_enable", default=False
    )
    caveats.extend(_ignored_caveats((*in_ignored, *sort_ignored)))

    directory, root_kind, needs, reachable = _screenshot_answer_root(
        root,
        reachable=reachable,
        in_content=in_content,
        sort_by_content=sort_by_content,
        content=content,
        sources=sources,
    )
    physical_dir: str | None = None
    if reachable and not needs:
        physical_dir, link_caveats = _link_view(machine, directory)
        caveats.extend(link_caveats)
    return ScreenshotPlacement(
        dir=directory,
        root_kind=root_kind,
        needs=needs,
        sources=tuple(sources),
        caveats=tuple(caveats),
        physical_dir=physical_dir,
    )


def _screenshot_answer_root(
    root: str | None,
    *,
    reachable: bool,
    in_content: bool,
    sort_by_content: bool,
    content: _Content,
    sources: list[str],
) -> tuple[str, ScreenshotRootKind, tuple[str, ...], bool]:
    """Which root the shot lands under, and the directory below it.

    RetroArch's own decision order (task_screenshot.c:488-550): the
    in-content flag outranks even a configured directory, an empty root
    falls to the content, and sorting by the content's directory name
    applies only under a configured root.
    """
    needs: tuple[str, ...] = ()
    if in_content or root is None:
        directory = content.dir_path or TEMPLATE_CONTENT_DIR
        if content.dir_path is None:
            needs = (HOLE_CONTENT_DIR,)
        sources.append(
            "screenshots land in the content's own directory — "
            + (
                "screenshots_in_content_dir is on, which outranks even a configured directory "
                "(task_screenshot.c:547-550)"
                if in_content
                else "no usable screenshot_directory stands, and an empty root falls to the "
                "content (task_screenshot.c:547-550); without content RetroArch refuses the "
                "shot entirely (:941-943)"
            )
        )
        return directory, SCREENSHOT_ROOT_CONTENT_DIRECTORY, needs, content.dir_path is not None
    directory = root
    if sort_by_content:
        # task_screenshot.c:494-507 — the parent directory's name, only
        # under a configured root.
        if content.dir_name is not None:
            directory = os.path.join(directory, content.dir_name)
        else:
            directory = os.path.join(directory, TEMPLATE_CONTENT_DIR_NAME)
            needs = (HOLE_CONTENT_DIR_NAME,)
        sources.append(
            "sorted into a subdirectory named after the content's directory "
            "(sort_screenshots_by_content_enable, task_screenshot.c:494-507); created at "
            "the moment of the shot (:553-556)"
        )
    return directory, SCREENSHOT_ROOT_DIRECTORY, needs, reachable


def _optional(caveat: Caveat | None) -> tuple[Caveat, ...]:
    """A caveat that may not exist, as something a caveat list can splice."""
    return () if caveat is None else (caveat,)


def _read_unestablished_caveat(core_key: str) -> Caveat | None:
    """Has anyone established that this core reads the directory below?

    At most one caveat, so the return type says so rather than a tuple that is
    empty or holds exactly one — a shape a caller has to unpack to learn what
    the signature could have told it.

    Driven by the audit, not by this function's knowledge of which cores are
    doubtful: three libretro cores port a standalone emulator and build their
    tree under a user directory whose root nobody has watched them choose, and
    that is the same open question the audit already carries for their saves
    (``core_audit.json``, verdict ``suspect``). So the doubt is read off the
    record. When the audit closes it — issue #98 — the verdict changes and this
    caveat retires or converts by that edit alone, exactly the way
    ``arrangement-unverified`` retires by an edit to
    ``arrangement_evidence.json``. No resolver names a core here, which is what
    keeps the retirement a data change rather than a code change.
    """
    entry = lookup_audit(core_key)
    if entry is None or entry.verdict != "suspect":
        return None
    return Caveat(
        CAVEAT_EMULATOR_READ_UNESTABLISHED,
        f"core {core_key!r} is a documented deviation suspect: which root it builds its user "
        "directory under has never been observed, so the directory below is the one the "
        "reading derives and nothing has confirmed the core reads it "
        "(docs/research/core-audit.md)",
        {"core": core_key, "verdict": entry.verdict},
    )


def _card_root(
    *,
    root: str,
    chain: _Chain,
    query: _SaveQuery,
) -> _SystemRoot:
    """The directory the core builds its content tree under, resolved live.

    Shared by both content-tree families, because the root a core builds a
    texture tree under and the root it builds a mod tree under are the same
    directory reached the same way — what differs is only the fragment below it.
    A second port of this would be a second answer to one question.

    Two kinds reach here, and each is resolved by the route that already owns
    it. ``system_directory`` is *the directory the core is handed*, not the cfg
    key's value (:func:`_core_system_root`) — so a machine with
    ``systemfiles_in_content_dir`` sends the packs to the content's own
    directory, and a caller who named no content is answered with the hole.
    ``savefile_directory`` is resolved by that family's own root selection, and
    for the same reason: ``savefiles_in_content_dir`` sends the core's save
    root to the content's own directory (``runloop.c:8789``), so a texture tree
    built under it goes there too. Reading the cfg key's value instead would
    name a directory RetroArch never hands the core — the mistake commit
    ``02177f2`` fixed for the system directory, in a second place. What is *not*
    taken from that family is the sorting stages — a derivation, decided
    against contrary evidence rather than settled: the sorting redirect runs
    before the core ever sees the directory
    (``docs/research/retrodeck-save-placement.md`` §"sorting stages apply
    before the core", the [V-live] Flycast observation), so a core that builds
    its texture tree lazily under the handed root *would* land under the
    sorted directory. What decides the other way here: both distributions wire
    the texture links at the UNSORTED root while running with content-sorting
    on, and both disable sort-by-core — so the wired tree is the unsorted one
    on every arrangement with a card, and the sorted reading is live only on a
    bare RetroArch, which already rides ``arrangement-unverified``. If a live
    run ever shows a texture tree under a sorted directory, this branch is the
    one to revisit.

    The return type is the system route's, reused rather than copied: what a
    texture root needs from a resolution is exactly what that one already
    carries — the base, the holes it left, whether it can be looked at from
    here, and its provenance. Only ``root_kind`` goes unread, because nothing
    asked this question which root it was.
    """
    if root == ROOT_SYSTEM_DIRECTORY:
        return _core_system_root(
            sandbox=query.sandbox,
            cfg_label=query.cfg_label,
            layers=chain.layers,
            content=chain.content,
            retroarch_config_dir=chain.retroarch_config_dir,
        )
    if chain.layout.in_content_dir:
        provenance = (
            f'{query.cfg_label} chain: {chain.layout.keys.in_content_dir} = "true" — the core\'s save '
            "root is the content's own directory, and it builds its user directory there "
            "(runloop.c:8789)"
        )
        if chain.content.dir_path is not None:
            return _SystemRoot(chain.content.dir_path, ROOT_CONTENT_DIRECTORY, sources=(provenance,))
        return _SystemRoot(
            TEMPLATE_CONTENT_DIR,
            ROOT_CONTENT_DIRECTORY,
            needs=(HOLE_CONTENT_DIR,),
            sources=(provenance,),
        )
    return _SystemRoot(
        chain.effective_root,
        ROOT_SAVEFILE_DIRECTORY,
        reachable=chain.reachable,
        sources=(
            f"{query.cfg_label} chain: the core builds its user directory under the save root "
            f"({chain.effective_root})",
        ),
    )


def _switch_absent(
    card: TextureCard, *, core_version: str | None
) -> tuple[bool, tuple[str, ...], tuple[Caveat, ...]]:
    """A feature this build compiles in and offers no way to switch on.

    ``enabled`` is stated as a fact about the binary rather than read from
    anywhere — the setting exists in the emulator's own vocabulary, nothing in
    the shipped build writes it, and no configuration on any machine can reach
    it. That is a stronger claim than every other answer in this family makes,
    so it is the one that is pinned to a core generation: the card names the
    build it was proven against — its own field rather than the audit's
    save-side record, which moves for unrelated reasons — and a machine running
    a different one gets the claim with ``unverified-version`` beside it rather
    than silently inheriting it. The comparison needs both sides to speak, exactly as the
    arrangement-level tripwire does — a core that reports no version is not
    compared, and silence there means *no drift established*, not *no drift*.
    """
    switch = card.absent_switch
    assert switch is not None  # the caller checked; this keeps the type honest
    caveats = [
        Caveat(
            CAVEAT_FEATURE_SWITCH_ABSENT,
            f"core {card.key!r} reads texture packs from this directory and this build offers no way "
            f"to switch that on: {switch.setting} is not a core option and no configuration reaches "
            "it, so replacement stays off until the core itself changes",
            {"core": card.key, "option_key": switch.setting},
        )
    ]
    if core_version is not None and switch.verified_core != core_version:
        caveats.append(
            Caveat(
                CAVEAT_UNVERIFIED_VERSION,
                f"that {card.key!r} offers no switch was established against core "
                f"{switch.verified_core}, and "
                f"this machine runs {core_version} — a build is exactly what could add one, so the "
                "claim is not carried across the difference unexamined",
                {
                    "core": card.key,
                    "verification": "drifted",
                    "core_verified": switch.verified_core,
                    "core_live": core_version,
                },
            )
        )
    return (
        switch.enabled,
        (
            f"texture card '{card.key}': {switch.setting} is not switchable in this build — "
            f"{switch.citation}",
        ),
        tuple(caveats),
    )


def _texture_enabled(
    machine: Machine,
    *,
    card: TextureCard,
    chain: _Chain,
    query: _SaveQuery,
    core_version: str | None,
) -> tuple[bool | None, tuple[str, ...], tuple[Caveat, ...]]:
    """Is replacement switched on right now — read the way RetroArch reads it?

    The card names the option and what its values mean; everything else is live.
    The options files are walked in RetroArch's own priority order (game
    ``.opt``, folder ``.opt``, per-core ``.opt``, then the global file), and a
    file that states nothing falls back to the default the **installed core**
    registers, which is a machine fact rather than a recorded one.

    ``None`` is the honest answer three ways, and none of them may be read as
    *off*: the card records no governing option, nothing states the option and
    the core declared no default (LRPS2 and Dolphin register options too late
    for the probe to capture, so this is a real state on a real machine), or the
    value that is set is one the record cannot interpret.
    """
    if card.absent_switch is not None:
        return _switch_absent(card, core_version=core_version)
    if card.option is None:
        return (
            None,
            (f"texture card '{card.key}': no option governs replacement — whether it is on is not stated",),
            (),
        )
    # Options the probe did not capture are ``None``, which is a probe
    # limitation and not a core that registers none (:mod:`atlas.machine`) —
    # the difference decides whether a missing entry means *this core does not
    # offer the option* or *nobody looked*, and both end in the same honest
    # ``None`` default below.
    registered = chain.core.info.options if chain.core.info is not None else None
    live_option = registered.get(card.option.setting) if registered is not None else None
    option_gates = _option_gates(
        chain.layers, sandbox=query.sandbox, retroarch_config_dir=chain.retroarch_config_dir
    )
    value, provenance, _, _ = _core_options_value(
        machine,
        override_config_dir=chain.gates.override_config_dir,
        global_file=option_gates.global_file,
        library_name=chain.core.library_name,
        content_dir_name=chain.content.dir_name,
        rom_stem=chain.content.rom_stem,
        option_key=card.option.setting,
        option_default=live_option.default if live_option is not None else None,
        game_specific_options=option_gates.game_specific_options,
        per_core_options=option_gates.per_core_options,
    )
    stated = option_gates.caveats
    if value is None:
        return None, (provenance,), stated
    if value not in card.option.values:
        return (
            None,
            (provenance,),
            (
                *stated,
                Caveat(
                    CAVEAT_UNKNOWN_OPTION_VALUE,
                    f'core option {card.option.setting} = "{value}" is not a value the recorded texture '
                    f"behaviour of core {card.key!r} knows — whether replacement is on is left "
                    "unstated rather than read as off",
                    {"core": card.key, "option_key": card.option.setting, "value": value},
                ),
            ),
        )
    return card.option.values[value], (provenance,), stated


def _retroarch_texture_pack_location(
    machine: Machine, query: _SaveQuery
) -> TexturePlacement | Unresolved:
    """Where this core reads texture packs: the shared chain, then its texture card.

    The chain is the savefile family's, read once and unchanged — the same
    global cfg, the same override layers, the same core identification — because
    the roots a texture tree hangs off are the roots that family already
    resolves, and reading them a second way would be a second answer to one
    question. What differs is everything after the root: the fragment below it,
    the option that gates it and the keying of the tree are per-core behaviour
    from :mod:`atlas.textures`, not RetroArch's path math.

    Two refusals, both typed. A core the machine established is not installed
    ends the question the way it ends the save question. A core with no texture
    card ends it differently and says so in its own code: nothing establishes
    where this emulator reads packs, which is a statement about atlas and never
    the claim that the emulator has none.
    """
    chain = _read_chain(machine, query, SAVEFILE_KEYS)
    if chain.core.not_installed is not None:
        return chain.core.not_installed

    so_basename = os.path.basename(query.core_so) if query.core_so is not None else None
    card = lookup_texture_card(so_basename=so_basename, library_name=chain.core.library_name)
    if card is None:
        return Unresolved(
            UNRESOLVED_TEXTURE_WIRING_UNESTABLISHED,
            f"where {so_basename or 'this emulator'} reads texture packs is not established — atlas "
            "carries no texture wiring for it, which says nothing about whether it has the feature: "
            "the packaged knowledge simply does not reach this core "
            "(docs/how-to-use.md, 'Where texture packs go')",
            {"core_so": so_basename} if so_basename is not None else {},
        )

    root = _card_root(root=card.root, chain=chain, query=query)
    directory = os.path.join(root.base, card.subdir)
    enabled, enabled_sources, enabled_caveats = _texture_enabled(
        machine,
        card=card,
        chain=chain,
        query=query,
        core_version=chain.core.info.library_version if chain.core.info is not None else None,
    )
    caveats = [
        *chain.caveats,
        *root.caveats,
        *enabled_caveats,
        *_optional(_read_unestablished_caveat(card.key)),
    ]
    physical_dir: str | None = None
    if root.reachable and not root.needs:
        physical_dir, link_caveats = _link_view(machine, directory)
        caveats.extend(link_caveats)
    return TexturePlacement(
        dir=directory,
        needs=root.needs,
        enabled=enabled,
        keying=card.keying,
        sources=(
            *chain.sources,
            *root.sources,
            f"texture card '{card.key}': the core reads packs from {card.subdir!r} below that root "
            f"— {card.provenance}",
            *enabled_sources,
            *(
                (f"texture card '{card.key}': keyed by {card.keying} — {card.keying_citation}",)
                if card.keying is not None
                else ()
            ),
        ),
        caveats=tuple(caveats),
        physical_dir=physical_dir,
    )


@dataclass(frozen=True, slots=True)
class _XdgHomes:
    """The two XDG bases a standalone emulator's own trees hang off.

    An arrangement supplies them; a standalone card names which of the two it
    wants. The pair exists because the emulators genuinely split — Dolphin's
    ``Load`` and Cemu's ``graphicPacks`` are data, PPSSPP's memory stick and
    DuckStation's tree are config — and because only an arrangement knows where
    the bases are. Inside a flatpak they are pinned (flatpak force-sets the
    ``XDG_*_HOME`` variables after every override; see the env composition
    section of ``docs/research/retrodeck-save-placement.md``), which is the
    whole reason these rows need no emulator config read to place.
    """

    data: str
    config: str
    # The flatpak app id the launch runs the emulator under, where it runs one
    # at all — ``None`` for an arrangement's own bundled build, for an AppImage
    # and for a host install. It travels with the bases because it answers the
    # same question they do: which installation of this emulator is being read.
    flatpak: str | None = None
    # Whether the launch happens inside *some* flatpak sandbox, which is not
    # the same question as which app id it runs under: an arrangement's own
    # bundled build has no id of its own and is sandboxed all the same. It
    # matters for the emulators that pick a root by whether XDG_CONFIG_HOME is
    # set, because inside a sandbox it always is (see the class docstring).
    xdg_pinned: bool = False

    def base(self, which: str) -> str:
        return self.data if which == XDG_DATA else self.config

    def emulator_root(self, which: str, token: str | None) -> str:
        """The emulator's own directory below one of the two bases.

        The name is the settings table's to state — once per emulator, and
        per installation where the name belongs to the build rather than to
        the emulator (#246) — so no resolver spells it out.
        """
        return os.path.join(
            self.base(which),
            emulator_settings.user_directory(token, flatpak=self.flatpak),
        )


def _pcsx2_folder_below_dataroot(
    raw: str | None,
    *,
    key: str,
    default: str,
    default_citation: str,
    data_root: str,
    spelled: str | None = None,
) -> tuple[str | None, str]:
    """``LoadPathFromSettings``' non-absolute outcomes, told apart the way the emulator tells them.

    Four readers open a ``[Folders]`` directory of PCSX2's — the texture
    root, the memory-card directory, the savestates directory, and the
    per-game settings layer's — and every one used to fold a
    present-but-empty line into "key absent". The emulator does not: ``GetStringValue`` falls to
    the compiled default only when the lookup FAILS (SettingsInterface.h:83-89
    at v2.6.3) — SimpleIni stores the empty value a ``Key =`` line carries and
    hands it back — and ``Path::Combine(DataRoot, "")`` is the **DataRoot
    itself**, because the combine strips trailing separators after appending
    the empty component (FileSystem.cpp:847-862). A relative value joins the
    DataRoot (LoadPathFromSettings, Pcsx2Config.cpp:2272-2278). An absolute
    value is the caller's to translate through the launch's sandbox, which
    this helper cannot know: it returns ``(None, sentence)`` for that case.
    *spelled* is the case-variant spelling actually found in the file, where
    the caller read the key the way SimpleIni matches it (#225).
    """
    shown = spelled if spelled is not None else key
    if raw is None:
        return os.path.join(data_root, default), (
            f"{key} is unset — the default {default} below the DataRoot governs "
            f"({default_citation})"
        )
    if raw == "":
        return data_root, (
            f'PCSX2.ini: [Folders] {shown} = "" — a present-but-empty key keeps its empty '
            "value (GetStringValue defaults only when the lookup fails, "
            'SettingsInterface.h:83-89 at v2.6.3) and Path::Combine(DataRoot, "") is the '
            "DataRoot itself (FileSystem.cpp:847-862), not the compiled default"
        )
    if not os.path.isabs(raw):
        return os.path.join(data_root, raw), (
            f'PCSX2.ini: [Folders] {shown} = "{raw}" — a relative value joins the DataRoot '
            "(LoadPathFromSettings, Pcsx2Config.cpp:2272-2278)"
        )
    return None, f'PCSX2.ini: [Folders] {shown} = "{raw}"'


def _pcsx2_texture_placement(
    machine: Machine,
    *,
    card: StandaloneTextureCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    extra_caveats: tuple[Caveat, ...] = (),
) -> TexturePlacement | Unresolved:
    """PCSX2's texture answer: the directory its configuration names, and the switch.

    Two things set this apart from a card that opens a fixed default. The
    directory is a configuration value — ``[Folders] Textures``, read through
    the helper the memory-card directory goes through (LoadPathFromSettings,
    Pcsx2Config.cpp:2272-2278 at v2.6.3), so an unset key means the compiled
    ``textures`` below the DataRoot, a relative one resolves against it, and
    an absolute one is translated through the launch's sandbox rather than
    trusted as a host path — the same translation the memory-card and states
    directories get, because all three are values the emulator wrote from
    inside the same sandbox.
    And ``enabled`` is a real read rather than ``None``: the switch is
    ``[EmuCore/GS] LoadTextureReplacements``, compiled default off, and
    nothing is scanned while it is off (GSTextureReplacements.cpp:391-393).

    ``dir`` is the **load stage**, not the root, and that is deliberate. The
    tree is staged twice below the root — ``<serial>/replacements`` is read,
    ``<serial>/dumps`` is written — so an answer naming only the root would
    send a caller placing a pack one level above everything that reads it. The
    serial is the running disc's, which atlas does not read out of content, so
    it stays a hole for the caller who knows it.
    """
    if card.directory is None:
        raise ValueError(
            f"texture card {card.token!r} states no directory and this resolver reads "
            "one — the card and the code shipped out of step"
        )
    settings = emulator_settings.settings_file(card.token, card.settings)
    data_root = homes.emulator_root(settings.bases[0], card.token)
    ini_path = settings.only(
        config_home=homes.base("config"), data_home=homes.base("data"), flatpak=homes.flatpak
    )
    result = machine.read_text(ini_path)
    if result.status not in (READ_OK, READ_MISSING):
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"PCSX2's configuration ({ini_path}) exists and could not be read — where it "
            "reads texture packs from, and whether it reads them at all, is unknowable here",
            {"emulator": card.token, "config": ini_path},
        )
    values = qt_ini.values(result.text) if result.status == READ_OK and result.text else {}
    setting = card.directory
    # The key the way the emulator matches it (#295): CSimpleIniA is ASCII
    # case-insensitive, so a case-variant spelling governs here as it does
    # in the running emulator (:func:`atlas.qt_ini.simpleini_value`).
    raw_dir, dir_spelled = _simpleini_value(values, setting.section, setting.key)
    resolved, _ = _pcsx2_folder_below_dataroot(
        raw_dir,
        key=setting.key,
        default=setting.default,
        default_citation=setting.citation,
        data_root=data_root,
        spelled=dir_spelled,
    )
    if resolved is None:
        assert raw_dir is not None  # only an absolute value leaves the helper unresolved
        host = sandbox.host(setting.key, raw_dir)
        if host.path is None:
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
                f"the texture directory PCSX2's configuration names ({raw_dir!r}) has no "
                f"spelling on this host — {ini_path} read fine, and nothing this answer "
                "could anchor at",
                {"emulator": card.token, "config": ini_path, "path": raw_dir},
            )
        root = host.path
    else:
        root = resolved
    directory = os.path.join(root, TEMPLATE_SAVE_ID, _PCSX2_TEXTURE_LOAD_STAGE)
    switch = card.switch
    if switch is None:
        raise ValueError(
            f"texture card {card.token!r} states no switch and this resolver reads one "
            "— the card and the code shipped out of step"
        )
    raw_switch, _ = _simpleini_value(values, switch.section, switch.key)
    parsed_switch = qt_ini.from_chars_bool(raw_switch)
    # The card's own default is atlas's word, not an ini value, so it keeps
    # its plain comparison; the live value goes through the emulator's reading.
    enabled = parsed_switch if parsed_switch is not None else switch.default == "true"
    rejected = _pcsx2_rejected_switch(card.token, switch, raw_switch, parsed_switch, enabled)
    per_game = _pcsx2_game_settings_caveats(
        machine,
        values,
        data_root,
        card.token,
        sandbox=sandbox,
        keys=(f"[{switch.section}] {switch.key}",),
        governs=_pcsx2_texture_governs(f"[{setting.section}] {setting.key}"),
        read_through=_PCSX2_TEXTURE_READ_THROUGH,
    )
    physical_dir, link_caveats = _link_view(machine, root)
    return TexturePlacement(
        dir=directory,
        needs=(HOLE_SAVE_ID,),
        enabled=enabled,
        keying=card.keying,
        sources=(
            f"texture card '{card.token}': the directory is [{setting.section}] "
            f"{setting.key} in the emulator's own configuration — {setting.citation}",
            f"texture card '{card.token}': replacement is [{switch.section}] {switch.key} "
            f"— {switch.citation}",
            *(
                (f"texture card '{card.token}': keyed by {card.keying} — {card.keying_citation}",)
                if card.keying is not None
                else ()
            ),
            f"texture card '{card.token}': {card.provenance}",
        ),
        caveats=(
            *extra_caveats,
            *link_caveats,
            *rejected,
            *per_game,
            Caveat(
                CAVEAT_FILENAMES_CONTENT_CONDITIONAL,
                "the directory is staged per game below the texture root: replacements are "
                f"read from <serial>/{_PCSX2_TEXTURE_LOAD_STAGE} and dumps written to "
                f"<serial>/{_PCSX2_TEXTURE_DUMP_STAGE}. Fill <save_id> with the disc's "
                "serial as PCSX2 reads it off the running game; the spelling is exact, "
                "because a wrongly-cased directory is warned about and left unused on a "
                "case-sensitive filesystem",
                {
                    "core": card.token,
                    "root": root,
                    "save_id": "the disc's serial, as PCSX2 reads it off the running game",
                    "load_stage": _PCSX2_TEXTURE_LOAD_STAGE,
                    "dump_stage": _PCSX2_TEXTURE_DUMP_STAGE,
                    "citation": (
                        "GSTextureReplacements.cpp:39-40, :262-265 and :400-412 at v2.6.3"
                    ),
                },
            ),
        ),
        physical_dir=(
            os.path.join(physical_dir, TEMPLATE_SAVE_ID, _PCSX2_TEXTURE_LOAD_STAGE)
            if physical_dir is not None
            else None
        ),
    )


def _pcsx2_rejected_switch(
    token: str,
    switch: TextureSetting,
    raw: str | None,
    parsed: bool | None,
    governing: bool,
) -> list[Caveat]:
    """A switch value the emulator cannot read as a boolean, stated rather than swallowed.

    ``GetBoolValue`` returns false without writing the caller's variable when
    ``FromChars<bool>`` yields nothing, so the compiled default keeps governing
    — the setting does *not* become false. The save route says this in an
    ``OptionReading`` provenance; a texture answer carries no readings, so the
    same fact needs a caveat or it is not said at all, and a user who wrote
    something into that key sees an answer that looks like the key is unset.
    """
    if raw is None or parsed is not None:
        return []
    return [
        Caveat(
            CAVEAT_CFG_VALUE_REJECTED,
            f'{switch.section}/{switch.key} = "{raw}" is not a value this emulator reads as a '
            "boolean — FromChars<bool> takes true/yes/on/1/enabled and false/no/off/0/disabled "
            "(StringUtil.h:178-197 at v2.6.3), and GetBoolValue leaves the caller's variable "
            "untouched when it yields nothing (INISettingsInterface.cpp:198-210), so the "
            f"compiled default {str(governing).lower()} governs; the setting does not become "
            "false because the value was unreadable",
            {"core": token, "key": f"{switch.section}/{switch.key}", "value": raw},
        )
    ]


# What every PCSX2 route citing the layer cites, written once so two answers
# cannot drift into two tellings of one fact.
_PCSX2_LAYER = (
    "UpdateGameSettingsLayer, VMManager.cpp:932-997 at PCSX2 v2.6.3 — the file is "
    "<serial>_<CRC>.ini below [Folders] GameSettings, with <CRC>.ini as the legacy "
    "spelling (GetGameSettingsPath, :774-781)"
)

# The door every layered PCSX2 key comes through. ``VMManager::LoadSettings``
# hands ``LoadCoreSettings`` whatever ``Host::GetSettingsInterface`` returns, and
# that is the LAYERED interface itself rather than its base layer — so every key
# ``Pcsx2Config::LoadSave`` reads can be answered differently for one game.
_PCSX2_LOAD_CORE = (
    "VMManager::LoadCoreSettings on Host::GetSettingsInterface, the layered interface "
    "itself — VMManager.cpp:598-607 and :645-648, Host.cpp:173-176"
)

# Why the directory half of every PCSX2 answer is immovable, and the reason so
# many PCSX2 answers stay silent here altogether. Established repo-wide at the
# pin rather than assumed: ``EmuFolders::LoadConfig`` is the only reader of the
# ``[Folders]`` keys, and it has exactly two call sites, both handed the base
# layer. No per-game file reaches any of them.
_PCSX2_FOLDERS_ARE_BASE_ONLY = (
    "EmuFolders::LoadConfig (Pcsx2Config.cpp:2280-2316) is the only reader of the [Folders] "
    "keys and is handed the base layer at both of its call sites (VMManager.cpp:552, :835)"
)

# PCSX2 has NO off switch for this layer, and that is a checked negative rather
# than an unasked question: at v2.6.3 nothing named ApplyGameSettings exists,
# and none of UpdateGameSettingsLayer's four call sites (VMManager.cpp:765,
# :1108, :1334, :1650) is conditional on a setting — the function branches only
# on a non-zero disc CRC (:935) and on the file existing (:938, :944). The two
# switches that look like it are not: [EmuCore] EnableGameFixes gates the game
# DATABASE's fixes (VMManager.cpp:703, :3295) and [EmuCore] EnablePatches /
# EnableCheats gate the pnach patch and cheat system (Patch.cpp:815, :831).
# So the statements below are unconditional, unlike DuckStation's
# (:func:`atlas.duckstation.applies_game_settings`).

# The door the texture switch itself comes through. Both halves of the texture
# answer are named from the CARD rather than written here, so a card that renamed
# either setting cannot leave this statement quietly describing the old one.
_PCSX2_TEXTURE_READ_THROUGH = (
    f"GSOptions::LoadSave, Pcsx2Config.cpp:908 and :1004, reached from {_PCSX2_LOAD_CORE}"
)


def _pcsx2_texture_governs(directory_key: str) -> str:
    """What a per-game file does to the texture answer — and the half it cannot touch."""
    return (
        "A per-game file can flip the switch this answer states. It cannot move the texture "
        f"directory that switch reads below: {directory_key} is a folder setting, and "
        f"{_PCSX2_FOLDERS_ARE_BASE_ONLY}."
    )


def _pcsx2_game_settings_caveats(
    machine: Machine,
    values: Mapping[tuple[str, str], str],
    data_root: str,
    token: str,
    *,
    sandbox: _Sandbox,
    keys: tuple[str, ...],
    governs: str,
    read_through: str,
) -> list[Caveat]:
    """The per-game ini layer, where this machine has one — PCSX2's second settings source.

    A running game installs ``<DataRoot>/gamesettings/<serial>_<crc>.ini`` as a
    *layer* under the settings interface every core setting is read through
    (:data:`_PCSX2_LAYER`), so a key of any section that ``Pcsx2Config::LoadSave``
    reads can be answered differently for one game than the global ``PCSX2.ini``
    answers it. The directory is the usual ``LoadPathFromSettings`` shape,
    ``[Folders] GameSettings`` defaulting to ``gamesettings`` below the DataRoot
    (Pcsx2Config.cpp:2290).

    What the layer reaches is not "the configuration" — it is exactly the keys
    read through :data:`_PCSX2_LOAD_CORE`. Every ``[Folders]`` key goes through
    a different door and is base-layer only, so the *directories* PCSX2's
    answers name cannot move at all. Saying "per-game overrides may be present"
    and leaving it there would invite a caller to distrust a directory nothing
    can move, which is why each caller passes the *keys* that answer depends
    on, a *read_through* naming the door those keys come through, and a
    *governs* sentence carrying its own citations for both halves — what a
    per-game value does here, and what it cannot touch.

    Which game runs is not a fact atlas holds, so the layer cannot be read
    *for* an answer — but whether one exists at all is a directory listing.
    Silent where the directory holds none, which is the shipped state, and
    silence means this answer holds for every game.

    A listing that *failed* is not that silence. The absence of a caveat here
    is what tells a caller this answer holds for every game, so answering an
    unreadable directory the way an empty one is answered claims exactly what
    was not established — which is why the failure has a code of its own. An
    absolute configured value is translated through the launch's sandbox like
    every other path this configuration names; one with no host spelling is
    that same unread state, because the listing cannot be made from here.

    Nothing here claims a key **is** set: a game ini may carry any section, so
    the honest statement is that these keys CAN be answered differently for a
    game this answer cannot name.
    """
    spelled = ", ".join(keys)
    plural = "keys" if len(keys) > 1 else "key"
    are = "are" if len(keys) > 1 else "is"
    raw, spelled_dir = _simpleini_value(values, "Folders", "GameSettings")
    resolved, _ = _pcsx2_folder_below_dataroot(
        raw,
        key="GameSettings",
        default="gamesettings",
        default_citation="Pcsx2Config.cpp:2290",
        data_root=data_root,
        spelled=spelled_dir,
    )
    if resolved is None:
        assert raw is not None  # only an absolute value leaves the helper unresolved
        host = sandbox.host("GameSettings", raw)
        if host.path is None:
            # An absolute value only the emulator's sandbox can spell: the
            # listing this caveat rests on cannot be made from here, and that
            # is the unread state — never the silent absence, whose meaning is
            # "no game carries an override". The cause rides beside the
            # consequence in the settled words: ``host.caveats`` is the
            # sandbox-path-untranslated caveat every other stands-around-it
            # site emits, so a client sees WHY the layer went unread and that
            # the stated directory is the configured sandbox spelling, not a
            # host directory that failed to glob.
            return [
                *host.caveats,
                Caveat(
                    CAVEAT_PER_GAME_LAYER_UNREAD,
                    f"[Folders] GameSettings = {raw!r} names a location only the emulator's "
                    "sandbox can spell, so whether any game on this machine carries a "
                    "per-game settings file is unknown — PCSX2 layers such a file over the "
                    f"whole configuration while that game runs ({_PCSX2_LAYER}), and the "
                    f"{plural} {spelled} would be read through it ({read_through}). {governs}",
                    {"core": token, "dir": raw, "key": spelled},
                ),
            ]
        directory = host.path
    else:
        directory = resolved
    listing = machine.glob(os.path.join(directory, _ANY_INI_GLOB))
    if listing.status != GLOB_COMPLETE:
        return [
            Caveat(
                CAVEAT_PER_GAME_LAYER_UNREAD,
                f"{directory} could not be listed, so whether any game on this machine carries "
                "a per-game settings file is unknown — PCSX2 layers such a file over the whole "
                f"configuration while that game runs ({_PCSX2_LAYER}), and the {plural} "
                f"{spelled} would be read through it ({read_through}). {governs}",
                {"core": token, "dir": directory, "key": spelled},
            )
        ]
    if not listing.matches:
        return []
    return [
        Caveat(
            CAVEAT_PER_GAME_OVERRIDES_PRESENT,
            f"{len(listing.matches)} game(s) on this machine carry a per-game settings file "
            f"in {directory}, which PCSX2 installs as a layer over the global configuration "
            f"while that game runs ({_PCSX2_LAYER}) — the {plural} {spelled} {are} read "
            f"through that layer ({read_through}), so this answer is the one that holds for "
            f"every game without such a file. {governs}",
            {
                "core": token,
                "count": str(len(listing.matches)),
                "dir": directory,
                "key": spelled,
            },
        )
    ]


def _duckstation_dataroot_caveat(token: str) -> Caveat:
    """The texture and mod routes' wording of :func:`atlas.duckstation.dataroot_caveat`."""
    return duckstation.dataroot_caveat(token, "the directory below")


def _duckstation_configured_directory(
    machine: Machine,
    *,
    token: str,
    setting: TextureSetting | ModSetting,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    extra_caveats: tuple[Caveat, ...],
    reads: str,
    named: str,
    switch: str,
) -> tuple[str, str | None, list[Caveat]] | Unresolved:
    """A ``[Folders]`` directory read the way DuckStation reads it: (dir, physical dir, caveats).

    The texture and cheat routes ask one question of one file, so they ask it
    once here. The root the key resolves against is the config home or the
    data home depending on how the launch was started, which is why neither
    row is a fixed XDG join; an unset key is the emulator's compiled default
    below that root, a relative value hangs off it, and an absolute one is
    translated through the launch's sandbox rather than trusted as a host
    path — the states directory beside them already reads this way. Neither
    card names a switch, so both answers close with
    ``emulator-config-unread`` naming the file that would hold one.

    Only the nouns differ between the two, so they are parameters the way
    :func:`_duckstation_settings` makes its refusal sentence one: ``reads`` is
    what the emulator reads from the directory ("texture packs"), ``named``
    the word the untranslatable refusal spells ("texture"), and ``switch`` the
    feature whose switch went unread ("texture replacement").
    """
    read = duckstation.read_settings(
        machine,
        config_home=homes.base("config"),
        data_home=homes.base("data"),
        flatpak=homes.flatpak,
        xdg_pinned=homes.xdg_pinned,
    )
    if read.unreadable is not None:
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"DuckStation's configuration ({read.unreadable}) exists and could not be read — "
            f"where it reads {reads} from is unknowable here",
            {"emulator": token, "config": read.unreadable},
        )
    # The key the way the emulator matches it (#295): CSimpleIniA is ASCII
    # case-insensitive, so a case-variant spelling governs here as it does
    # in the running emulator (:func:`atlas.qt_ini.simpleini_value`).
    configured = _simpleini_value(read.values, setting.section, setting.key)[0] or ""
    if os.path.isabs(configured):
        host = sandbox.host(setting.key, configured)
        if host.path is None:
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
                f"the {named} directory DuckStation's configuration names ({configured!r}) "
                f"has no spelling on this host — {read.stated_path} read fine, and nothing "
                "this answer could anchor at",
                {"emulator": token, "config": read.stated_path or "", "path": configured},
            )
        directory = host.path
    else:
        directory = duckstation.load_path(
            read.values, read.root, setting.section, setting.key, setting.default
        )
    physical_dir, link_caveats = _link_view(machine, directory)
    caveats: list[Caveat] = [*extra_caveats, *link_caveats]
    if read.ambiguous:
        caveats.append(_duckstation_dataroot_caveat(token))
    config_path = os.path.join(read.root, duckstation.CONFIG_FILENAME)
    caveats.append(
        Caveat(
            CAVEAT_EMULATOR_CONFIG_UNREAD,
            f"whether {token} has {switch} switched on is not established — the setting lives "
            f"in {config_path}, which this answer reads for the directory and not for the "
            "switch, because the card states none",
            {"emulator": token, "config": config_path},
        )
    )
    return directory, physical_dir, caveats


def _duckstation_texture_placement(
    machine: Machine,
    *,
    card: StandaloneTextureCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    extra_caveats: tuple[Caveat, ...] = (),
) -> TexturePlacement | Unresolved:
    """DuckStation's texture directory: ``[Folders] Textures``, below the root its launch picks.

    The reading its cheat tree gets, from the same file and for the same
    reason — see :func:`_duckstation_configured_directory`, which both routes
    read through. ``enabled`` stays unstated: the card names no switch, so
    nothing is read for one.
    """
    setting = card.directory
    if setting is None:
        raise ValueError(
            f"texture card {card.token!r} states no directory and this resolver reads "
            "one — the card and the code shipped out of step"
        )
    resolved = _duckstation_configured_directory(
        machine,
        token=card.token,
        setting=setting,
        homes=homes,
        sandbox=sandbox,
        extra_caveats=extra_caveats,
        reads="texture packs",
        named="texture",
        switch="texture replacement",
    )
    if isinstance(resolved, Unresolved):
        return resolved
    directory, physical_dir, caveats = resolved
    return TexturePlacement(
        dir=directory,
        needs=(),
        enabled=None,
        keying=card.keying,
        sources=(
            f"texture card '{card.token}': the directory is [{setting.section}] {setting.key} in "
            f"the emulator's own configuration — {setting.citation}",
            f"texture card '{card.token}': {card.provenance}",
        ),
        caveats=tuple(caveats),
        physical_dir=physical_dir,
    )


# The two stages PCSX2 keeps below a game's texture directory — the emulator's
# own spellings (GSTextureReplacements.cpp:39-40 at v2.6.3), and both whole
# strings in the shipped binary.
_PCSX2_TEXTURE_LOAD_STAGE = "replacements"
_PCSX2_TEXTURE_DUMP_STAGE = "dumps"

# Standalone texture cards whose directory is a configuration value rather than
# a fixed subpath. Keyed by token like the save resolvers, and a card stating a
# directory setting without one here fails loudly rather than answering a
# default nobody read.
_STANDALONE_TEXTURE_RESOLVERS = {
    "PCSX2": _pcsx2_texture_placement,
    "DUCKSTATION": _duckstation_texture_placement,
}


def _standalone_texture_placement(
    machine: Machine,
    *,
    card: StandaloneTextureCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    extra_caveats: tuple[Caveat, ...] = (),
) -> TexturePlacement | Unresolved:
    """Where a standalone emulator reads texture packs — an XDG join, then the links.

    Two shapes, and the card says which. Where it names a fixed subpath, no
    config of the emulator's is read and the answer says so: the directory is
    its own default below a base the arrangement pins, so it resolves without
    modelling the emulator, while the switch beside it does not. ``enabled``
    is then always ``None``, with ``emulator-config-unread`` naming the file
    that would answer it — never ``False``, which would be a reading nobody
    made. Where the card names a configuration key instead, its registered
    resolver reads that configuration the way the emulator does, and answers
    both the directory and the switch.

    ``needs`` is empty for the fixed shape: nothing in that join comes from
    the content, and the same directory serves every game the emulator
    launches.
    """
    if card.directory is not None:
        resolver = _STANDALONE_TEXTURE_RESOLVERS.get(card.token)
        if resolver is None:
            raise ValueError(
                f"standalone texture card {card.token!r} states a directory setting but has "
                "no resolver registered — the card and the code shipped out of step"
            )
        return resolver(
            machine, card=card, homes=homes, sandbox=sandbox, extra_caveats=extra_caveats
        )
    if card.base is None or card.subdir is None:
        raise ValueError(
            f"texture card {card.token!r} states no base/subdir pair and this resolver "
            "opens one — the card and the code shipped out of step"
        )
    directory = os.path.join(homes.emulator_root(card.base, card.token), card.subdir)
    config_path = emulator_settings.settings_file(card.token, card.settings).only(
        config_home=homes.base("config"), data_home=homes.base("data"), flatpak=homes.flatpak
    )
    physical_dir, link_caveats = _link_view(machine, directory)
    return TexturePlacement(
        dir=directory,
        needs=(),
        enabled=None,
        keying=card.keying,
        sources=(
            f"texture card '{card.token}': the emulator reads packs from {card.subdir!r} below "
            f"its own {emulator_settings.user_directory(card.token, flatpak=homes.flatpak)!r} "
            f"directory in the XDG {card.base} home — {card.provenance}",
            *(
                (f"texture card '{card.token}': keyed by {card.keying} — {card.keying_citation}",)
                if card.keying is not None
                else ()
            ),
        ),
        caveats=(
            *extra_caveats,
            *link_caveats,
            Caveat(
                CAVEAT_EMULATOR_CONFIG_UNREAD,
                f"whether {card.token} has texture replacement switched on is not established — the "
                f"setting lives in {config_path}, a configuration of the emulator's own that atlas "
                "does not read (standalone emulator configuration is its own roadmap block)",
                {"emulator": card.token, "config": config_path},
            ),
            # The Dolphin family reads this tree through a directory index one
            # per-game key re-points, so the fixed join above is the answer for
            # every game that carries no such file. Empty for every other token.
            *_dolphin_game_settings_caveats(
                machine,
                token=card.token,
                homes=homes,
                keys=_DOLPHIN_LOAD_LAYER_KEYS,
                governs="the Load directory this texture tree hangs below",
            ),
        ),
        physical_dir=physical_dir,
    )


def _standalone_texture_unresolved(spec: EmulatorSpec) -> Unresolved:
    """The refusal for a standalone entry no packaged card covers.

    The same code the save routes answer every standalone entry with, and
    deliberately so: what is missing here is what is missing there — nothing
    reads this emulator's own configuration, and for these emulators the
    texture directory is *in* that configuration rather than at a default the
    emulator picks (RetroDECK writes PCSX2's into ``PCSX2.ini`` and Vita3K's
    ``pref-path`` into ``config.yml``). An emulator whose packs live at its own
    default answers instead, and the split between the two is evidence, not
    policy.
    """
    return Unresolved(
        UNRESOLVED_STANDALONE,
        f"where standalone emulator {spec.label!r} ({spec.system}) reads texture packs is not "
        "resolvable yet — its texture directory is named in a configuration of its own, and "
        "reading those is the standalone roadmap block (ROADMAP.md)",
        {"label": spec.label, "system": spec.system},
    )


# ---------------------------------------------------------------------------
# Standalone save resolution — the savefile question for a catalogue entry
# whose command names an emulator a standalone save card covers. The card is
# the thin, cited half (atlas/standalone_saves.py); the reading here follows
# the emulator's own code at the pinned revision, and every value it takes
# off the machine is a config read or a symlink walk, never a guess.
# ---------------------------------------------------------------------------

# One card's citations, already narrowed to the installation this launch
# runs — what the readings below call to name a source. Binding it once at the
# entry point is what keeps a helper from having to know which build it is
# describing, and from being able to get it wrong.
_Cite = Callable[[str], str]


def _cites(card: StandaloneSaveCard, homes: _XdgHomes) -> _Cite:
    def cite(slot: str) -> str:
        return card.cite(slot, flatpak=homes.flatpak)

    return cite


# Dolphin 2603a, and its fork PrimeHack: one reading serves both, because the
# fork inherits the shape whole — the same EXI ids, the same registered slot
# defaults, the same compile-time region directories and card file stems, the
# same NAND tree. What it does not inherit is the line numbers, so every
# source this reading names in an answer comes from the card that is being
# read (``card.cite``) rather than from here.
_DOLPHIN_CITATION_SLOTS = frozenset(
    {
        "build",  # the release label an answer says its evidence is "at"
        "slot_devices",  # the EXI device ids a slot key spells
        "slot_defaults",  # what an unset SlotA/SlotB falls back to
        "session_overrides",  # the GCIFolder*PathOverride keys a session sets
        "gci_names",  # how a .gci file inside a folder card is named
        "nand_tree",  # the Wii NAND's title/<hi>/<lo>/data shape
        "wii_dir",  # the NAND's default directory below the user directory
    }
)


@dataclass(frozen=True, slots=True)
class _DolphinGameLayer:
    """One build's evidence for the per-game ini layer, in that build's own lines.

    Dolphin and its PrimeHack fork load **two** per-game layers over the whole
    configuration while a game runs, and both outrank ``Dolphin.ini``. They sit
    in different places and only one of them is a directory atlas can list:

    * ``LocalGame`` reads ``<user directory>/GameSettings/`` — the XDG data tree
      every other Dolphin answer already resolves, so its contents are a
      listing, counted like PCSX2's (:func:`_pcsx2_game_settings_caveats`).
    * ``GlobalGame`` reads ``GetSysDirectory() + GameSettings/`` — the build's
      own tree, and ``GetSysDirectory()`` is the compile-time ``DATA_DIR "sys/"``
      that is written **nowhere on a running machine**. Nothing atlas reads
      spells it, so it is stated and never counted.

    Which is why this record lives in code rather than on a card: it is the
    emulator's own source, shared by the save, texture and mod answers alike,
    and no one of those cards owns it. What it must NOT do is state one build's
    lines for another's answer, which is why it is keyed by the same
    (token, flatpak app id) identity the cards use — PrimeHack's RetroDECK
    build and the Flathub one are three years apart.
    """

    name: str
    build: str
    # ``Load``: the GlobalGame branch opens the build's Sys tree, the else
    # branch the user's directory.
    loader: str
    # Every mapped key of every section is set into the layer; only ``Save``
    # filters. This is what makes the reach the whole key space.
    unfiltered: str
    # LocalGame > GlobalGame > CommandLine > Base.
    order: str
    # ``GetSysDirectory()`` is the compile-time ``DATA_DIR "sys/"``.
    sys_dir: str


# Two line sets, not four: PrimeHack's Flathub build is a later rebase onto
# modern Dolphin and carries Dolphin's line numbers, while the revision
# RetroDECK builds is three years older and carries its own. Which set a launch
# gets is decided by the build it runs, never by the emulator's name.
#
# Only what a message actually says is data here. Two more facts were read at
# these same pins and belong in the record without being fields no sentence
# surfaces:
#
#   * the user directory's join, ``D_GAMESETTINGS_IDX = D_USER_IDX +
#     GAMESETTINGS_DIR DIR_SEP`` — FileUtil.cpp:843 modern, :840 fork. The
#     caveats name that directory by its resolved path, not by its citation,
#     because it is a path atlas really walked.
#   * the reachability proof — the layers are installed at
#     ConfigManager.cpp:254-255 (modern) / :189-190 (fork), reached from
#     BootManager.cpp:56 / :65, which runs before ``Core::Init`` and therefore
#     before the emulated hardware opens a memory card or the video backend
#     opens the Load tree. Adding a layer fires the config-changed callback
#     that re-runs ``InitCustomPaths`` (UICommon.cpp:102-118, :131-135 modern;
#     :101-118, :130 fork), which is how one ``LoadPath`` line moves both the
#     texture tree and the graphics-mod tree (FileUtil.cpp:967-972 / :946-950).
#     This is why the caveats are stated at all rather than hedged: the layer
#     is provably in place before every read they qualify.
_DOLPHIN_MODERN_LINES = {
    "loader": "GameConfigLoader.cpp:185-197",
    "unfiltered": (
        "GameConfigLoader.cpp:261-277, the IsSettingSaveable filter being Save's own at :294"
    ),
    "order": "Enums.h:39-47",
    "sys_dir": "FileUtil.cpp:760-793",
}
_DOLPHIN_FORK_LINES = {
    "loader": "GameConfigLoader.cpp:176-198",
    "unfiltered": (
        "GameConfigLoader.cpp:253-272, the IsSettingSaveable filter being Save's own at :292"
    ),
    "order": "Enums.h:40-48",
    "sys_dir": "FileUtil.cpp:758-791",
}

# The tokens this layer is established for. Kept beside the table rather than
# derived from it, because the two answer different questions: this one says
# "does this emulator load a per-game layer at all" (a fact about the
# emulator), the table says "which build's lines describe it" (a fact about the
# installation). An emulator missing from here is silent; one missing only a
# build row raises.
_DOLPHIN_FAMILY = frozenset({"DOLPHIN", "PRIMEHACK"})

# Keyed the way every standalone registry is: the token, and the flatpak app id
# where the build differs per installation (#246). ``None`` covers an
# arrangement's own bundled build. Stating 81bfb96's lines for the Flathub
# PrimeHack would be exactly the mistake the cards' ``citations.installations``
# block exists to prevent, which is why that row is keyed separately.
_DOLPHIN_GAME_LAYERS: dict[tuple[str, str | None], _DolphinGameLayer] = {
    ("DOLPHIN", None): _DolphinGameLayer(
        name="Dolphin", build="dolphin 2603a", **_DOLPHIN_MODERN_LINES
    ),
    ("DOLPHIN", "org.DolphinEmu.dolphin-emu"): _DolphinGameLayer(
        name="Dolphin", build="dolphin 2603a", **_DOLPHIN_MODERN_LINES
    ),
    ("PRIMEHACK", None): _DolphinGameLayer(
        name="PrimeHack", build="shiiion/dolphin 81bfb96", **_DOLPHIN_FORK_LINES
    ),
    ("PRIMEHACK", "io.github.shiiion.primehack"): _DolphinGameLayer(
        name="PrimeHack", build="shiiion/dolphin 53f53e0", **_DOLPHIN_MODERN_LINES
    ),
}

# The user directory's per-game tree, below the emulator's own XDG data root —
# the LocalGame layer's source (FileUtil.cpp:843 at dolphin 2603a).
_DOLPHIN_GAME_SETTINGS_DIR = "GameSettings"


def _dolphin_game_settings_caveats(
    machine: Machine,
    *,
    token: str,
    homes: _XdgHomes,
    keys: tuple[str, ...],
    governs: str,
) -> list[Caveat]:
    """The per-game ini layers, stated beside a Dolphin-family answer.

    Two facts, and a caller has to be able to tell them apart, so they are two
    codes. The **user** directory is a listing atlas makes: where files sit
    there, ``per-game-overrides-present`` says how many and where, and where the
    listing fails ``per-game-layer-unread`` says the check did not happen — the
    PCSX2 vocabulary unchanged, because the situation is unchanged. The
    **build's** directory is not a listing atlas can make at all: its location
    is compiled into the binary, so ``per-game-build-layer-unread`` states it
    and no count of it will ever appear.

    That second caveat is unconditional, and that is the point. Under the PCSX2
    shape, silence means "this answer holds for every game" — and for a
    Dolphin-family emulator that would be false whatever the user's directory
    holds, because the build ships a layer of its own that is read regardless.
    So a Dolphin answer is never silent here.

    Neither caveat claims a key is actually set. Nothing filters what a game ini
    may carry (the loader sets every mapped key; only ``Save`` filters), so the
    honest statement is that these keys CAN be answered differently for a game
    this answer cannot name — never that any file does so.

    *keys* are section-qualified the way a game ini must spell them: the
    memory-card keys live in ``[Core]``, but ``NANDRootPath`` and ``LoadPath``
    are reached through the free-form ``<System>.<Section>`` parse, and the
    name that parse resolves for ``System::Main`` is ``Dolphin`` — so the
    section is ``[Dolphin.General]``, and ``[Main.General]`` is dropped with a
    warning. *governs* says, in the answer's own terms, what those keys move.
    """
    if token not in _DOLPHIN_FAMILY:
        return []
    layer = _DOLPHIN_GAME_LAYERS.get((token, homes.flatpak))
    if layer is None:
        # A Dolphin-family emulator whose build has no row is card/code drift,
        # and it fails loudly the way every other drift in this file does
        # (a texture card naming a directory setting with no resolver
        # registered raises exactly here). Falling back to another build's row
        # would state that build's line numbers for this one — the mistake the
        # per-installation citations exist to prevent — and returning nothing
        # would drop the statement SILENTLY, which is the failure this whole
        # answer exists to remove. ``test_every_dolphin_family_build_has_a_row``
        # keeps it unreachable for the builds the cards actually name.
        raise ValueError(
            f"{token!r} runs under flatpak {homes.flatpak!r}, a build the per-game layer "
            "table states no source lines for — the cards and the code shipped out of step"
        )
    spelled = ", ".join(keys)
    plural = "keys" if len(keys) > 1 else "key"
    are = "are" if len(keys) > 1 else "is"
    directory = os.path.join(
        homes.emulator_root(XDG_DATA, token), _DOLPHIN_GAME_SETTINGS_DIR
    )
    built_in = Caveat(
        CAVEAT_PER_GAME_BUILD_LAYER_UNREAD,
        f"{layer.name} loads a second per-game layer from its own build — the GameSettings "
        f"directory below the Sys tree, whose location is compiled into the binary "
        f"({layer.sys_dir} at {layer.build}) and written nowhere on a running machine, so "
        f"this answer never lists it and states no count for it. Nothing filters what such a "
        f"file may set ({layer.unfiltered}), so the {plural} {spelled} — {governs} — can be "
        f"answered differently there for a game this answer cannot name; the user's own "
        f"{directory} outranks it ({layer.order})",
        {"core": token, "key": spelled, "layer": "GlobalGame"},
    )
    # What the USER's directory has to say comes first, where it says anything:
    # it is the layer that outranks the build's, so a reader meets the stronger
    # statement first. The build's rides behind it unconditionally. One
    # accumulator and one return, because the result is a homogeneous SEQUENCE
    # of caveats and not a record whose positions mean things — the arity is the
    # count of statements this machine earned, never a shape a caller unpacks.
    caveats: list[Caveat] = []
    listing = machine.glob(os.path.join(directory, _ANY_INI_GLOB))
    if listing.status != GLOB_COMPLETE:
        caveats.append(
            Caveat(
                CAVEAT_PER_GAME_LAYER_UNREAD,
                f"{directory} could not be listed, so whether any game on this machine carries "
                f"a per-game settings file of its own is unknown — {layer.name} layers such a "
                f"file over the whole configuration while that game runs, above every value "
                f"Dolphin.ini states ({layer.loader}, {layer.order} at {layer.build}), and the "
                f"{plural} {spelled} — {governs} — would be read through it",
                {"core": token, "dir": directory, "key": spelled},
            )
        )
    elif listing.matches:
        caveats.append(
            Caveat(
                CAVEAT_PER_GAME_OVERRIDES_PRESENT,
                f"{len(listing.matches)} game(s) on this machine carry a per-game settings file "
                f"in {directory}, which {layer.name} layers over the whole configuration while "
                f"that game runs, above every value Dolphin.ini states ({layer.loader}, "
                f"{layer.order} at {layer.build}) — the {plural} {spelled} — {governs} — {are} "
                f"read through that layer, so this answer is the one that holds for every game "
                f"without such a file",
                {
                    "core": token,
                    "count": str(len(listing.matches)),
                    "dir": directory,
                    "key": spelled,
                },
            )
        )
    caveats.append(built_in)
    return caveats


# The section-qualified keys each Dolphin-family answer depends on. The memory
# card keys map through the legacy section table ([Core] -> {Main, "Core"});
# the General ones only through the free-form <System>.<Section> parse, whose
# name for System::Main is "Dolphin" — [Main.General] resolves to nothing.
_DOLPHIN_GC_LAYER_KEYS = (
    "[Core] MemcardAPath",
    "[Core] MemcardBPath",
    "[Core] GCIFolderAPath",
    "[Core] GCIFolderBPath",
)
_DOLPHIN_WII_LAYER_KEYS = ("[Dolphin.General] NANDRootPath",)
# One key moves both the texture tree and the graphics-mod tree: it re-points
# D_LOAD_IDX, and the rebuild recomputes D_HIRESTEXTURES_IDX and
# D_GRAPHICSMOD_IDX below it (FileUtil.cpp:967-972 at dolphin 2603a).
_DOLPHIN_LOAD_LAYER_KEYS = ("[Dolphin.General] LoadPath",)

_DOLPHIN_REGIONS = ("USA", "EUR", "JAP")
_DOLPHIN_REGION_SPELLINGS = ("USA", "EUR", "JAP", "JPN", "DEV")
_DOLPHIN_DEVICE_RAW = 1
_DOLPHIN_DEVICE_FOLDER = 8
_DOLPHIN_DEVICE_AGP = 9
_DOLPHIN_DEVICE_NONE = 255
_DOLPHIN_SLOT_DEFAULTS = {"A": _DOLPHIN_DEVICE_FOLDER, "B": _DOLPHIN_DEVICE_NONE}


def _parse_sectioned_ini(text: str) -> dict[tuple[str, str], str]:
    """``key = value`` lines under ``[section]`` headers — Dolphin.ini, kept as written.

    The mapping keeps the file's own spellings in file order; *matching* is
    the lookup's job, and it is ASCII case-insensitive with the last
    occurrence winning, because that is how the emulator reads this file
    (#295): sections are found by ``CaseInsensitiveEquals`` and case-variant
    headers merge (IniFile.cpp:130-146, :289 at dolphin 2603a), keys live in
    a ``CaseInsensitiveLess`` map where a duplicate's last value wins
    (IniFile.h:64; ``insert_or_assign``, IniFile.cpp:47-49 from the parse at
    :308), and the config layer the values land in keys them by
    ``strcasecmp`` on section and key (BaseConfigLoader.cpp:144-181,
    Layer.h:56, ConfigInfo.cpp:18-29) — the identical chain at PrimeHack's
    pins (shiiion/dolphin@81bfb96 IniFile.h:89, @53f53e0 IniFile.h:64). An
    exact-duplicate key collapses at parse here the way it does upstream;
    case-variant duplicates stay separate entries and the lookup
    (:func:`atlas.qt_ini.simpleini_value`) takes the last in file order.
    """
    parsed: dict[tuple[str, str], str] = {}
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if sep:
            parsed[(section, key.strip())] = value.strip()
    return parsed


@dataclass(frozen=True, slots=True)
class _DolphinSlot:
    """One card slot's contribution to the answer: groups, readings, caveats."""

    mode: str
    groups: tuple[FileGroup, ...] = ()
    readings: tuple[OptionReading, ...] = ()
    caveats: tuple[Caveat, ...] = ()
    template_dir: str | None = None


def _dolphin_region_split(value: str, *, separator: str) -> tuple[str, str]:
    """*value* with a trailing region code stripped — ``(base, suffix-after-region)``.

    The emulator's own move, made explicit: a configured card path is a
    template whose region segment the running disc replaces (GetMemcardPath
    MainSettings.cpp:777-819, GetGCIFolderPath :844-873, at dolphin 2603a —
    the fork's own lines live on the card, per the note above
    ``_DOLPHIN_CITATION_SLOTS``). A value that spells no region keeps its
    tail and the region is inserted before it.
    """
    for region in _DOLPHIN_REGION_SPELLINGS:
        marker = separator + region
        if value.endswith(marker):
            return value[: -len(marker)], ""
    return value, ""


def _dolphin_raw_slot(
    letter: str,
    configured: str | None,
    sandbox: _Sandbox,
    gc_root: str,
    cite: "_Cite",
    *,
    spelled: str | None = None,
) -> _DolphinSlot:
    """A raw memory card in one slot: one file per region, named.

    An empty path defaults to ``<GC user>/MemoryCard<slot>.<region>.raw``
    (MainSettings.cpp:767-774 at dolphin 2603a); a standard-size card
    carries no block suffix (EXI.cpp:123-126 sizes the card at 2043 blocks
    unless MemoryCardSize overrides it, and the suffix only exists below
    that, MainSettings.cpp:763-765).
    """
    if configured:
        resolved = sandbox.host(f"Memcard{letter}Path", configured)
        if resolved.path is None:
            return _DolphinSlot(
                mode="card",
                caveats=(
                    Caveat(
                        CAVEAT_SANDBOX_PATH_UNTRANSLATED,
                        f"Dolphin.ini sets Memcard{letter}Path to {configured!r}, a path only the "
                        "emulator's sandbox can read — the card file could not be located from here",
                        {"key": f"Memcard{letter}Path", "path": configured},
                    ),
                ),
                readings=(
                    _dolphin_reading(f"Memcard{letter}Path", configured, None, cite, spelled=spelled),
                ),
            )
        directory, filename = os.path.split(resolved.path)
        stem, ext = os.path.splitext(filename)
        base, _ = _dolphin_region_split(stem, separator=".")
        names = tuple(f"{base}.{region}{ext}" for region in _DOLPHIN_REGIONS)
    else:
        directory = gc_root
        names = tuple(f"MemoryCard{letter}.{region}.raw" for region in _DOLPHIN_REGIONS)
    return _DolphinSlot(
        mode="card",
        groups=tuple(
            FileGroup(dir=directory, files=(name,), granularity="shared-file", role="memory-card")
            for name in names
        ),
        readings=(
            _dolphin_reading(f"Memcard{letter}Path", configured, None, cite, spelled=spelled),
        ),
    )


def _dolphin_folder_slot(
    letter: str,
    configured: str | None,
    sandbox: _Sandbox,
    gc_root: str,
    cite: "_Cite",
    *,
    spelled: str | None = None,
) -> _DolphinSlot:
    """A GCI folder in one slot: one directory per region, files unnamed.

    An empty path defaults to ``<GC user>/<region>/Card <slot>``
    (MainSettings.cpp:835-841 at dolphin 2603a). The ``.gci`` names inside
    come from the save's own directory entry — makercode, gamecode and the
    save's internal filename (GCMemcardDirectory.cpp:56-60) — none of which
    any read of the content path recovers, so the groups stay unnamed.
    """
    if configured:
        resolved = sandbox.host(f"GCIFolder{letter}Path", configured)
        if resolved.path is None:
            return _DolphinSlot(
                mode="folder",
                caveats=(
                    Caveat(
                        CAVEAT_SANDBOX_PATH_UNTRANSLATED,
                        f"Dolphin.ini sets GCIFolder{letter}Path to {configured!r}, a path only the "
                        "emulator's sandbox can read — the folder could not be located from here",
                        {"key": f"GCIFolder{letter}Path", "path": configured},
                    ),
                ),
                readings=(
                    _dolphin_reading(f"GCIFolder{letter}Path", configured, None, cite, spelled=spelled),
                ),
            )
        base, _ = _dolphin_region_split(resolved.path.rstrip("/"), separator="/")
        dirs = tuple(os.path.join(base, region) for region in _DOLPHIN_REGIONS)
        template = os.path.join(base, TEMPLATE_REGION)
    else:
        dirs = tuple(os.path.join(gc_root, region, f"Card {letter}") for region in _DOLPHIN_REGIONS)
        template = os.path.join(gc_root, TEMPLATE_REGION, f"Card {letter}")
    return _DolphinSlot(
        mode="folder",
        groups=tuple(
            FileGroup(dir=d, files=None, granularity="per-game-files", role="memory-card")
            for d in dirs
        ),
        readings=(
            _dolphin_reading(f"GCIFolder{letter}Path", configured, None, cite, spelled=spelled),
        ),
        template_dir=template,
    )


def _dolphin_reading(
    key: str,
    value: str | None,
    provenance: str | None,
    cite: "_Cite | None" = None,
    *,
    spelled: str | None = None,
) -> OptionReading:
    """One key's reading: the canonical key, and the file's own spelling in the sentence.

    *spelled* is the case-variant spelling the file carries, where a value was
    found — the reading's ``key`` stays canonical (it is what a client selects
    by), and the provenance quotes the line as written, the way the
    DuckStation/PCSX2 readings and the NANDRootPath site already do (#295).
    An unset key has no file spelling, so the default sentence keeps the
    canonical one.
    """
    default_provenance = f"[Core] {key} is unset — the compiled-in default governs"
    if cite is not None:
        default_provenance += f" ({cite('slot_defaults')} at {cite('build')})"
    shown = spelled if spelled is not None else key
    return OptionReading(
        key,
        value,
        provenance
        or (f'Dolphin.ini: [Core] {shown} = "{value}"' if value is not None else default_provenance),
        None,
    )


def _dolphin_slot(
    letter: str,
    values: Mapping[tuple[str, str], str],
    sandbox: _Sandbox,
    gc_root: str,
    cite: "_Cite",
) -> _DolphinSlot:
    """One slot read the way the emulator reads it: device id first, then the path.

    Every key is matched ASCII case-insensitively, last occurrence winning,
    because that is the emulator's own matching (the chain is on
    :func:`_parse_sectioned_ini`, #295).
    """
    raw_value, slot_spelled = _simpleini_value(values, "Core", f"Slot{letter}")
    try:
        device = int(raw_value) if raw_value is not None else _DOLPHIN_SLOT_DEFAULTS[letter]
    except ValueError:
        device = None
    slot_reading = _dolphin_reading(f"Slot{letter}", raw_value, None, cite, spelled=slot_spelled)
    if device == _DOLPHIN_DEVICE_NONE:
        return _DolphinSlot(mode="none", readings=(slot_reading,))
    if device == _DOLPHIN_DEVICE_FOLDER:
        folder_value, folder_spelled = _simpleini_value(values, "Core", f"GCIFolder{letter}Path")
        slot = _dolphin_folder_slot(
            letter,
            folder_value,
            sandbox,
            gc_root,
            cite,
            spelled=folder_spelled,
        )
    elif device == _DOLPHIN_DEVICE_RAW:
        card_value, card_spelled = _simpleini_value(values, "Core", f"Memcard{letter}Path")
        slot = _dolphin_raw_slot(
            letter,
            card_value,
            sandbox,
            gc_root,
            cite,
            spelled=card_spelled,
        )
    elif device == _DOLPHIN_DEVICE_AGP:
        return _DolphinSlot(
            mode="agp",
            readings=(slot_reading,),
            caveats=(
                Caveat(
                    CAVEAT_CORE_MODE_UNESTABLISHED,
                    f"Dolphin's slot {letter} holds a GBA cartridge adapter (AGP, EXI device 9) — "
                    "its saves go onto the cartridge image the emulator is configured with, which "
                    "this answer does not model; the other slot's statement stands on its own",
                    {"core": "DOLPHIN", "reason": f"slot {letter} is an AGP device"},
                ),
            ),
        )
    else:
        return _DolphinSlot(
            mode="unknown",
            readings=(slot_reading,),
            caveats=(
                Caveat(
                    CAVEAT_CORE_MODE_UNESTABLISHED,
                    f'Dolphin.ini sets Slot{letter} to "{raw_value}", a device this card cannot '
                    "interpret — what sits in that slot and where it saves is unestablished",
                    {"core": "DOLPHIN", "reason": f'Slot{letter} = "{raw_value}" is uninterpreted'},
                ),
            ),
        )
    return _DolphinSlot(
        mode=slot.mode,
        groups=slot.groups,
        readings=(slot_reading, *slot.readings),
        caveats=slot.caveats,
        template_dir=slot.template_dir,
    )


def _dolphin_gc_answer(
    slots: tuple[_DolphinSlot, _DolphinSlot],
    *,
    machine: Machine,
    ini_path: str | None,
    card: StandaloneSaveCard,
    cite: _Cite,
    extra_caveats: tuple[Caveat, ...],
) -> SavefilePlacement:
    """The GameCube answer assembled from both slots' contributions."""
    groups = tuple(g for slot in slots for g in slot.groups)
    readings = tuple(
        _reading_with_file(r, ini_path) for slot in slots for r in slot.readings
    )
    caveats = [*extra_caveats, *(c for slot in slots for c in slot.caveats)]
    mode = "+".join(slot.mode for slot in slots)
    template = next((slot.template_dir for slot in slots if slot.template_dir), None)
    if groups:
        directory = template or groups[0].dir
        needs = (HOLE_REGION,) if template else ()
        named_first = groups[0].files is not None
        files = tuple(
            name for g in groups if g.dir == groups[0].dir and g.files for name in g.files
        ) if named_first else ()
        state = FILE_SET_DECLARED
        if any(g.files is None for g in groups):
            caveats.extend(
                Caveat(
                    CAVEAT_FILE_NAMES_UNESTABLISHED,
                    "the .gci files here are named from the save's own directory entry — "
                    "makercode, gamecode and the save's internal filename — which follow from "
                    "nothing atlas reads; back the directory up whole",
                    {
                        "core": card.token,
                        "dir": g.dir,
                        "role": g.role,
                        "citation": f"{cite('gci_names')} at {cite('build')}",
                    },
                )
                for g in groups
                if g.files is None
            )
    else:
        # No device keeps a card: nothing on this machine takes a GameCube
        # game's save writes until a slot is configured again.
        directory = template or (ini_path and os.path.dirname(ini_path)) or "/"
        needs = ()
        files = ()
        state = FILE_SET_DECLARED
        caveats.append(
            Caveat(
                CAVEAT_SAVE_WRITES_DISCARDED,
                "no memory card sits in either slot (Dolphin.ini [Core] SlotA/SlotB) — a "
                "GameCube game finds nowhere to save and nothing is kept; the granularity "
                "block names the switches that would change that",
                {"core": card.token, "mode": mode},
            )
        )
    physical, link_caveats = (
        _link_view(machine, directory) if TEMPLATE_REGION not in directory else (None, ())
    )
    caveats.extend(link_caveats)
    return SavefilePlacement(
        dir=directory,
        root_kind=ROOT_EMULATOR_DIRECTORY,
        needs=needs,
        fallback_dir=None,
        file_set=FileSet(
            state,
            files,
            f"declared by standalone save card '{card.token}'",
            complete=False,
            groups=groups,
        ),
        sources=(f"standalone save card '{card.token}': {card.provenance}",),
        caveats=tuple(caveats),
        physical_dir=physical,
        granularity=Granularity(
            value=groups[0].granularity if groups else GRANULARITY_NONE,
            mode=mode,
            readings=readings,
            alternatives=_dolphin_alternatives(slots),
            provenance=(
                f"standalone save card '{card.token}': mode {mode!r} from Dolphin.ini's slot "
                f"devices ({cite('slot_devices')} at {cite('build')})"
            ),
        ),
    )


def _reading_with_file(reading: OptionReading, options_file: str | None) -> OptionReading:
    return OptionReading(reading.key, reading.value, reading.provenance, options_file)


# The one edit a player actually makes: flip slot A between the folder
# (per-game files) and the raw card (one shared file per region). Keyed by
# the mode it flips FROM; slot B's combinations multiply the space without
# changing the shape of any answer, so they stay as they are.
_DOLPHIN_SLOT_A_FLIPS = {
    "folder": ("card", _DOLPHIN_DEVICE_RAW, "shared-file"),
    "card": ("folder", _DOLPHIN_DEVICE_FOLDER, "per-game-files"),
}


def _dolphin_alternatives(
    slots: tuple[_DolphinSlot, _DolphinSlot]
) -> tuple[ModeAlternative, ...]:
    """The other card scheme for slot A — every other mode is a caveat, not a mode."""
    a, b = slots
    alternatives: list[ModeAlternative] = []
    flip = _DOLPHIN_SLOT_A_FLIPS.get(a.mode)
    if flip is not None:
        other, device, value = flip
        alternatives.append(
            ModeAlternative(
                mode=f"{other}+{b.mode}",
                options=(("SlotA", str(device)),),
                values=(value,),
            )
        )
    return tuple(alternatives)


def _unnamed_tree_placement(
    card: StandaloneSaveCard,
    *,
    directory: str,
    mode: str,
    readings: tuple[OptionReading, ...],
    caveats: tuple[Caveat, ...],
    physical: str | None,
    provenance: str,
) -> SavefilePlacement:
    """One unnamed per-game tree as a whole answer — the shape three cards share.

    A Wii NAND, a PSP memstick's savedata and a Wii U MLC make the same claim:
    the tree is stated, its entries are named by the game and refused, and the
    granularity is per-game-files with the readings that located the tree.
    """
    return SavefilePlacement(
        dir=directory,
        root_kind=ROOT_EMULATOR_DIRECTORY,
        needs=(),
        fallback_dir=None,
        file_set=FileSet(
            FILE_SET_DECLARED,
            (),
            f"declared by standalone save card '{card.token}'",
            complete=False,
            groups=(
                FileGroup(dir=directory, files=None, granularity="per-game-files", role=ROLE_BATTERY),
            ),
        ),
        sources=(f"standalone save card '{card.token}': {card.provenance}",),
        caveats=caveats,
        physical_dir=physical,
        granularity=Granularity(
            value="per-game-files",
            mode=mode,
            readings=readings,
            alternatives=(),
            provenance=provenance,
        ),
    )


def _dolphin_wii_answer(
    values: Mapping[tuple[str, str], str],
    *,
    machine: Machine,
    sandbox: _Sandbox,
    homes: _XdgHomes,
    ini_path: str | None,
    card: StandaloneSaveCard,
    extra_caveats: tuple[Caveat, ...],
) -> SavefilePlacement:
    """The Wii answer: the NAND's title tree, one unnamed directory per title.

    ``NANDRootPath`` governs where the NAND lives (the default is the ``Wii``
    tree below the user directory, CommonPaths.h:49 at 2603a); the saves
    inside it are ``title/<hi:08x>/<lo:08x>/data`` (NandPaths.cpp:63-71 at
    2603a), the title id a fact of the disc that no read of the content path
    recovers. The key is matched the way the emulator matches it — ASCII
    case-insensitively (the chain is on :func:`_parse_sectioned_ini`, #295).
    """
    cite = _cites(card, homes)
    configured, spelled = _simpleini_value(values, "General", "NANDRootPath")
    caveats = [*extra_caveats]
    if configured:
        resolved = sandbox.host("NANDRootPath", configured)
        nand_root = resolved.path
        reading = _dolphin_reading("NANDRootPath", configured, f'Dolphin.ini: [General] {spelled} = "{configured}"')
        if nand_root is None:
            caveats.append(
                Caveat(
                    CAVEAT_SANDBOX_PATH_UNTRANSLATED,
                    f"Dolphin.ini sets NANDRootPath to {configured!r}, a path only the emulator's "
                    "sandbox can read — falling back to the default NAND root it spells",
                    {"key": "NANDRootPath", "path": configured},
                )
            )
            nand_root = os.path.join(homes.emulator_root(XDG_DATA, card.token), "Wii")
    else:
        nand_root = os.path.join(homes.emulator_root(XDG_DATA, card.token), "Wii")
        reading = _dolphin_reading(
            "NANDRootPath",
            None,
            "[General] NANDRootPath is unset — the NAND defaults to the Wii tree below the "
            f"user directory ({cite('wii_dir')} at {cite('build')})",
        )
    directory = os.path.join(nand_root, "title")
    # The link walk stops at the NAND root: the ``title`` tree below it is
    # created at the first Wii save, so its absence is no dead link — the
    # root's own resolution is the boundary the arrangement reroutes.
    resolved_root, link_caveats = _link_view(machine, nand_root)
    physical = os.path.join(resolved_root, "title") if resolved_root else None
    caveats.extend(link_caveats)
    caveats.append(
        Caveat(
            CAVEAT_FILE_NAMES_UNESTABLISHED,
            "a Wii save lives in title/<title id>/data below the NAND root, and the title id "
            "is the disc's own — it follows from nothing atlas reads; back the tree up whole",
            {
                "core": card.token,
                "dir": directory,
                "role": ROLE_BATTERY,
                "citation": f"{cite('nand_tree')} at {cite('build')}",
            },
        )
    )
    return _unnamed_tree_placement(
        card,
        directory=directory,
        mode="nand",
        readings=(_reading_with_file(reading, ini_path),),
        caveats=tuple(caveats),
        physical=physical,
        provenance=(
            f"standalone save card '{card.token}': the Wii NAND tree "
            f"({cite('nand_tree')} at {cite('build')})"
        ),
    )


def _dolphin_savefile_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavefilePlacement | Unresolved:
    """Dolphin's save answer, read from Dolphin.ini the way the emulator reads it.

    Shared with PrimeHack, which is Dolphin with a different name for its own
    directory: the shape is inherited whole, and everything this reading says
    about its source comes from the card being read, in the build this launch
    runs (:func:`_cites`).
    """
    cite = _cites(card, homes)
    ini_path = _standalone_settings_path(card, homes)
    result = machine.read_text(ini_path)
    if result.status not in (READ_OK, READ_MISSING):
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"Dolphin's configuration ({ini_path}) exists and could not be read — which devices "
            "sit in the card slots and where the trees point is unknowable here",
            {"emulator": card.token, "config": ini_path},
        )
    values = _parse_sectioned_ini(result.text) if result.status == READ_OK and result.text else {}
    stated_ini = ini_path if result.status == READ_OK else None
    if system == "wii":
        return _dolphin_wii_answer(
            values,
            machine=machine,
            sandbox=sandbox,
            homes=homes,
            ini_path=stated_ini,
            card=card,
            extra_caveats=(
                *extra_caveats,
                *_dolphin_game_settings_caveats(
                    machine,
                    token=card.token,
                    homes=homes,
                    keys=_DOLPHIN_WII_LAYER_KEYS,
                    governs="where the Wii NAND lives",
                ),
            ),
        )
    gc_root = os.path.join(homes.emulator_root(XDG_DATA, card.token), "GC")
    override_caveats = tuple(
        Caveat(
            CAVEAT_CORE_MODE_UNESTABLISHED,
            f"Dolphin.ini carries {key}, a per-session override a movie or netplay session "
            f"sets ({cite('session_overrides')}) — while one runs, the cards live at its "
            "path, not at the answer's",
            {"core": card.token, "reason": f"{key} is set"},
        )
        for key in ("GCIFolderAPathOverride", "GCIFolderBPathOverride")
        if _simpleini_value(values, "Core", key)[0]
    )
    slots = (
        _dolphin_slot("A", values, sandbox, gc_root, cite),
        _dolphin_slot("B", values, sandbox, gc_root, cite),
    )
    return _dolphin_gc_answer(
        slots,
        machine=machine,
        ini_path=stated_ini,
        card=card,
        cite=cite,
        extra_caveats=(
            *extra_caveats,
            *override_caveats,
            *_dolphin_game_settings_caveats(
                machine,
                token=card.token,
                homes=homes,
                keys=_DOLPHIN_GC_LAYER_KEYS,
                governs="which file or folder each memory card slot reads",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# PPSSPP v1.20.4 — the memstick is fixed by the build on Linux: the config
# tree itself ($XDG_CONFIG_HOME/ppsspp, NativeApp.cpp:473-482), no setting
# names it. Savedata is <memstick>/PSP/SAVEDATA (PathUtil.cpp:52, :62-63),
# one directory per game, named by the game itself out of its own id and
# save name — read from nothing atlas touches.
# ---------------------------------------------------------------------------


def _ppsspp_savefile_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavefilePlacement | Unresolved:
    """PPSSPP's save answer — a compiled-in XDG join, then the links."""
    directory = os.path.join(homes.emulator_root(XDG_CONFIG, card.token), "PSP", "SAVEDATA")
    physical, link_caveats = _link_view(machine, directory)
    caveats = [
        *extra_caveats,
        *link_caveats,
        Caveat(
            CAVEAT_FILE_NAMES_UNESTABLISHED,
            "a PSP save is one directory per game below SAVEDATA, named by the game itself "
            "from its own id and save name — it follows from nothing atlas reads; back the "
            "tree up whole",
            {
                "core": card.token,
                "dir": directory,
                "role": ROLE_BATTERY,
                "citation": "the game names its savedata directory; the tree is "
                "<memstick>/PSP/SAVEDATA (PathUtil.cpp:52,:62-63 at ppsspp v1.20.4)",
            },
        ),
    ]
    return _unnamed_tree_placement(
        card,
        directory=directory,
        mode="memstick",
        readings=(),
        caveats=tuple(caveats),
        physical=physical,
        provenance=(
            f"standalone save card '{card.token}': the memstick is fixed by the build "
            "(NativeApp.cpp:473-482 at v1.20.4) — no switch selects anything"
        ),
    )


def _standalone_settings(card: StandaloneSaveCard) -> emulator_settings.SettingsFile:
    """The settings file this save card names, from the one table that addresses it."""
    if card.settings is None:
        raise ValueError(
            f"save card {card.token!r} names no settings file and this resolver reads "
            "one — the card and the code shipped out of step"
        )
    return emulator_settings.settings_file(card.token, card.settings)


def _standalone_settings_path(card: StandaloneSaveCard, homes: _XdgHomes) -> str:
    """Where this launch opens the file the card names."""
    return _standalone_settings(card).only(
        config_home=homes.base("config"), data_home=homes.base("data"), flatpak=homes.flatpak
    )


# ---------------------------------------------------------------------------
# xemu v0.8.135 — every game's save lives on the emulated Xbox hard disk,
# a qcow2 image xemu.toml names ([sys.files] hdd_path, config_spec.yml:
# 359-363), beside the EEPROM file (eeprom_path) that holds the console's
# settings. Per-title structure exists inside the image's FATX filesystem —
# one directory per title id under UDATA/ — but nothing outside the image
# is addressable, which the save-inside-image caveat states machine-readably.
# The card names that configuration under the *data* home, which is where the
# emulator opens it (SDL_GetPrefPath); an arrangement that keeps the real
# directory elsewhere and links it there is walked like any other symlink.
# ---------------------------------------------------------------------------


def _xemu_launch_dependent_caveat(core: str, key: str, value: str) -> Caveat:
    """The relative-value rider: xemu opens the value from the launch's own cwd.

    A relative ``[sys.files]`` value is composed verbatim into the QEMU machine
    options (system/vl.c:2983-3095 at v0.8.135) and opened with plain POSIX
    calls — ``xemu_check_file`` is ``qemu_fopen`` (vl.c:2527-2535), the EEPROM
    probe ``qemu_access`` (vl.c:2918), and both are ``fopen``/``access``
    outside Windows (include/qemu/osdep.h:645-653) — while no step of the
    launch changes the process's directory (``main``, ui/xemu.c:1278-1379).
    So the anchor is the launching process's working directory: a property of
    the launch, not of the machine, exactly the melonDS relative-path fact.
    """
    return Caveat(
        CAVEAT_SAVE_DIR_LAUNCH_DEPENDENT,
        f"{key} is the relative value {value!r}, which xemu opens relative to the "
        "working directory of the launching process (the configured string is passed "
        "verbatim into the QEMU options, system/vl.c:2983-3095, and opened with plain "
        "fopen/access, vl.c:2527-2535 and :2918 with osdep.h:645-653, at v0.8.135) — "
        "a property of the launch, not of the machine; fill 'cwd' with the launcher's "
        "working directory to complete the path",
        {"core": core, "key": key, "path": value},
    )


def _cwd_templated(directory: str) -> bool:
    """Is this directory the launch's own — the ``<cwd>`` template or below it?"""
    return directory == TEMPLATE_CWD or directory.startswith(TEMPLATE_CWD + "/")


def _xemu_group(
    sandbox: _Sandbox, key: str, value: str, *, role: str, core: str
) -> tuple[FileGroup | None, tuple[Caveat, ...]]:
    if not os.path.isabs(value):
        head, name = os.path.split(value)
        directory = os.path.join(TEMPLATE_CWD, head) if head else TEMPLATE_CWD
        return (
            FileGroup(dir=directory, files=(name,), granularity="shared-file", role=role),
            (_xemu_launch_dependent_caveat(core, key, value),),
        )
    resolved = sandbox.host(key, value)
    if resolved.path is None:
        return None, (
            Caveat(
                CAVEAT_SANDBOX_PATH_UNTRANSLATED,
                f"xemu.toml sets {key} to {value!r}, a path only the emulator's sandbox can "
                "read — the file could not be located from here",
                {"key": key, "path": value},
            ),
        )
    directory, name = os.path.split(resolved.path)
    return FileGroup(dir=directory, files=(name,), granularity="shared-file", role=role), ()


def _xemu_document(
    machine: Machine,
    card: "StandaloneSaveCard | StandaloneSavestateCard",
    toml_path: str,
    *,
    lost: str,
) -> "tuple[Mapping[str, Any], str | None] | Unresolved":
    """xemu.toml parsed, or the refusal — no frame exists to step aside to.

    Shared by the save and the savestate readings (#284): both open the same
    file the same way, and each hands in *lost* — the sentence naming what
    ITS question can no longer answer — so a savestate refusal never talks
    about the EEPROM, which is the save question's business alone.
    """
    result = machine.read_text(toml_path)
    if result.status not in (READ_OK, READ_MISSING):
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"xemu's configuration ({toml_path}) exists and could not be read — {lost}",
            {"emulator": card.token, "config": toml_path},
        )
    try:
        doc: Mapping[str, Any] = (
            tomllib.loads(result.text) if result.status == READ_OK and result.text else {}
        )
    except tomllib.TOMLDecodeError:
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"xemu's configuration ({toml_path}) is not parseable TOML — {lost}",
            {"emulator": card.token, "config": toml_path},
        )
    return doc, (toml_path if result.status == READ_OK else None)


def _xemu_disk_pieces(
    sandbox: _Sandbox, card: StandaloneSaveCard, hdd: str | None
) -> tuple[tuple[FileGroup, ...], tuple[Caveat, ...]]:
    """The hard-disk image's group and what travels with it — or why there is none."""
    if not hdd:
        return (), (
            Caveat(
                CAVEAT_CORE_MODE_UNESTABLISHED,
                "xemu.toml names no hard-disk image ([sys.files] hdd_path) — the machine has "
                "no disk to save onto, and where one would be attached is unknowable here",
                {"core": card.token, "reason": "hdd_path is unset"},
            ),
        )
    group, group_caveats = _xemu_group(
        sandbox, "hdd_path", hdd, role=ROLE_BATTERY, core=card.token
    )
    if group is None or not group.files:
        return (), group_caveats
    return (group,), (
        *group_caveats,
        Caveat(
            CAVEAT_SAVE_INSIDE_IMAGE,
            f"every game's save lives inside {group.files[0]} — the emulated Xbox hard disk, "
            "a FATX filesystem with one directory per title id under UDATA/ — and nothing "
            "outside the image is addressable per game: back the image up whole, or parse "
            "its filesystem with the layout stated here",
            {
                "emulator": card.token,
                "image": os.path.join(group.dir, group.files[0]),
                "layout": "UDATA/<title id>",
            },
        ),
    )


def _xemu_readings(
    hdd: str | None, eeprom: str | None, stated_toml: str | None
) -> tuple[OptionReading, ...]:
    return (
        _reading_with_file(
            _dolphin_reading(
                "hdd_path",
                hdd,
                f'xemu.toml: [sys.files] hdd_path = "{hdd}"'
                if hdd
                else "[sys.files] hdd_path is unset — no hard-disk image is configured",
            ),
            stated_toml,
        ),
        _reading_with_file(
            _dolphin_reading(
                "eeprom_path",
                eeprom,
                f'xemu.toml: [sys.files] eeprom_path = "{eeprom}"'
                if eeprom
                else "[sys.files] eeprom_path is unset",
            ),
            stated_toml,
        ),
    )


def _xemu_savefile_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavefilePlacement | Unresolved:
    """xemu's save answer, read from xemu.toml the way the emulator reads it."""
    toml_path = _standalone_settings_path(card, homes)
    parsed = _xemu_document(
        machine,
        card,
        toml_path,
        lost="where the hard-disk image and the EEPROM live is unknowable here",
    )
    if isinstance(parsed, Unresolved):
        return parsed
    doc, stated_toml = parsed
    hdd = xemu_file_value(doc, "hdd_path")
    eeprom = xemu_file_value(doc, "eeprom_path")
    readings = _xemu_readings(hdd, eeprom, stated_toml)
    disk_groups, disk_caveats = _xemu_disk_pieces(sandbox, card, hdd)
    eeprom_group, eeprom_caveats = (
        _xemu_group(sandbox, "eeprom_path", eeprom, role=ROLE_SETTINGS, core=card.token)
        if eeprom
        else (None, ())
    )
    groups = [*disk_groups, *([eeprom_group] if eeprom_group is not None else [])]
    caveats = [*extra_caveats, *disk_caveats, *eeprom_caveats]
    if not groups:
        # Two different empties: the config named files and none has a spelling
        # on this host (the untranslatable-path refusal, first stated path
        # named), or it named nothing at all for this question to anchor at.
        untranslated = tuple(
            str(c.data["path"])
            for c in (*disk_caveats, *eeprom_caveats)
            if c.code == CAVEAT_SANDBOX_PATH_UNTRANSLATED
        )
        if untranslated:
            # ``path`` stays the primary — the hard-disk image, where the
            # saves live — and ``paths`` lists every untranslatable value in
            # this emitter's stated order (the disk image first, then the
            # EEPROM) whenever more than one file is named.
            data: dict[str, str | tuple[str, ...]] = {
                "emulator": card.token,
                "config": toml_path,
                "path": untranslated[0],
            }
            if len(untranslated) > 1:
                data["paths"] = untranslated
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
                f"the files xemu's configuration names ({', '.join(untranslated)}) have "
                f"no spelling on this host — {toml_path} read fine, and nothing this "
                "answer could anchor at",
                data,
            )
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"none of the files xemu's configuration names could be located from here "
            f"({toml_path}) — nothing this answer could anchor at",
            {"emulator": card.token, "config": toml_path},
        )
    directory = groups[0].dir
    # A <cwd>-templated directory is a property of the launch — nothing on the
    # machine exists to walk links on — while a hole anywhere in the answer
    # (the EEPROM's group can be the templated one under an absolute disk)
    # belongs in ``needs``: the answer's holes, not the primary directory's.
    launch_anchored = _cwd_templated(directory)
    if launch_anchored:
        physical = None
    else:
        physical, link_caveats = _link_view(machine, directory)
        caveats.extend(link_caveats)
    files = tuple(
        name for g in groups if g.dir == directory and g.files for name in g.files
    )
    return SavefilePlacement(
        dir=directory,
        root_kind=ROOT_WORKING_DIRECTORY if launch_anchored else ROOT_EMULATOR_DIRECTORY,
        needs=(HOLE_CWD,) if any(_cwd_templated(g.dir) for g in groups) else (),
        fallback_dir=None,
        file_set=FileSet(
            FILE_SET_DECLARED,
            files,
            f"declared by standalone save card '{card.token}'",
            complete=False,
            groups=tuple(groups),
        ),
        sources=(f"standalone save card '{card.token}': {card.provenance}",),
        caveats=tuple(caveats),
        physical_dir=physical,
        granularity=Granularity(
            value=groups[0].granularity,
            mode="hdd",
            readings=tuple(readings),
            alternatives=(),
            provenance=(
                f"standalone save card '{card.token}': the image and the EEPROM from "
                "xemu.toml ([sys.files], config_spec.yml:359-363 at v0.8.135)"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Cemu 2.6 — the Wii U's internal storage (MLC) holds every save, and where
# the MLC lives is: a --mlc launch flag, else settings.xml's mlc_path, else
# <user data>/mlc01 (ActiveSettings.cpp:242-268). The save tree inside it is
# usr/save/<title id hi>/<title id lo>/user/ — the shipped binary carries
# that scheme as the whole literal 'usr/save/{:08X}/{:08X}/user/' — one
# subtree per title, the id the game's own.
# ---------------------------------------------------------------------------


def _cemu_mlc_root(
    doc: "_ET.Element | None", homes: _XdgHomes, sandbox: _Sandbox, token: str, xml_path: str
) -> tuple[str | None, OptionReading, Unresolved | None]:
    """The MLC root the way Cemu resolves it — configured, or the default."""
    configured = None
    if doc is not None:
        element = doc.find("mlc_path")
        if element is not None and element.text and element.text.strip():
            configured = element.text.strip()
    if configured is None:
        return (
            os.path.join(homes.emulator_root(XDG_DATA, token), "mlc01"),
            _dolphin_reading(
                "mlc_path",
                None,
                "settings.xml names no mlc_path — the MLC defaults to mlc01 below Cemu's "
                "user data (ActiveSettings.cpp:265-268 at 2.6)",
            ),
            None,
        )
    reading = _dolphin_reading("mlc_path", configured, f'settings.xml: mlc_path = "{configured}"')
    resolved = sandbox.host("mlc_path", configured)
    if resolved.path is None:
        refusal = Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
            f"the MLC path Cemu's configuration names ({configured!r}) has no spelling "
            f"on this host — {xml_path} read fine, and nothing this answer could "
            "anchor at",
            {"emulator": token, "config": xml_path, "path": configured},
        )
        return None, reading, refusal
    return resolved.path, reading, None


def _cemu_savefile_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavefilePlacement | Unresolved:
    """Cemu's save answer, read from settings.xml the way the emulator reads it."""
    xml_path = _standalone_settings_path(card, homes)
    result = machine.read_text(xml_path)
    if result.status not in (READ_OK, READ_MISSING):
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"Cemu's configuration ({xml_path}) exists and could not be read — where the MLC "
            "and every save in it live is unknowable here",
            {"emulator": card.token, "config": xml_path},
        )
    doc: _ET.Element | None = None
    if result.status == READ_OK and result.text:
        try:
            doc = _ET.fromstring(result.text)
        except _ET.ParseError:
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
                f"Cemu's configuration ({xml_path}) is not parseable XML — where the MLC and "
                "every save in it live is unknowable here",
                {"emulator": card.token, "config": xml_path},
            )
    caveats: list[Caveat] = [*extra_caveats]
    if "--mlc" in command:
        caveats.append(
            Caveat(
                CAVEAT_CORE_MODE_UNESTABLISHED,
                "the launch command carries an --mlc flag, which outranks settings.xml "
                "(ActiveSettings.cpp:242-251 at 2.6) — the tree below may not be the one "
                "this launch uses",
                {"core": card.token, "reason": "an --mlc launch flag outranks the config"},
            )
        )
    mlc_root, reading, root_refusal = _cemu_mlc_root(doc, homes, sandbox, card.token, xml_path)
    if root_refusal is not None:
        return root_refusal
    assert mlc_root is not None  # the helper resolves a root wherever it does not refuse
    tree = os.path.join(mlc_root, "usr", "save")
    physical_tree, link_caveats = _link_view(machine, tree)
    caveats.extend(link_caveats)
    # The per-title unit: one directory per game below usr/save, keyed by the
    # title id — Cemu composes usr/save/<high>/<low>/user/<account>/ (and
    # user/common/) at its runtime write sites, lowercase hex
    # (nn_save.cpp:133-145 at 2.6). The id is the game's own, read from
    # nothing atlas touches, so it stays a hole the caller fills — as two
    # 8-hex path segments, which is one fact (the title id), one hole.
    directory = os.path.join(tree, TEMPLATE_SAVE_ID)
    physical = (
        os.path.join(physical_tree, TEMPLATE_SAVE_ID) if physical_tree is not None else None
    )
    caveats.append(
        Caveat(
            CAVEAT_FILE_NAMES_UNESTABLISHED,
            "the files below a title's save directory are the game's own writes "
            "(user/<account>/ and user/common/) — move the directory whole; fill "
            "<save_id> with the title id, high word then low word, each 8 lowercase "
            "hex digits, as two path segments",
            {
                "core": card.token,
                "dir": directory,
                "role": ROLE_BATTERY,
                "save_id": "the Wii U title id: <high 8 hex>/<low 8 hex>, lowercase",
                "citation": "nn_save.cpp:133-145 at Cemu 2.6 — "
                "'/vol/storage_mlc01/usr/save/%08x/%08x/user/%08x/' and 'user/common/'",
            },
        )
    )
    stated_xml = xml_path if result.status == READ_OK else None
    return SavefilePlacement(
        dir=directory,
        root_kind=ROOT_EMULATOR_DIRECTORY,
        needs=(HOLE_SAVE_ID,),
        fallback_dir=None,
        file_set=FileSet(
            FILE_SET_DECLARED,
            (),
            f"declared by standalone save card '{card.token}'",
            complete=False,
            groups=(
                FileGroup(
                    dir=directory,
                    files=None,
                    granularity=GRANULARITY_PER_GAME_DIRECTORY,
                    role=ROLE_BATTERY,
                ),
            ),
        ),
        sources=(f"standalone save card '{card.token}': {card.provenance}",),
        caveats=tuple(caveats),
        physical_dir=physical,
        granularity=Granularity(
            value=GRANULARITY_PER_GAME_DIRECTORY,
            mode="mlc",
            readings=(_reading_with_file(reading, stated_xml),),
            alternatives=(),
            provenance=(
                f"standalone save card '{card.token}': the MLC from settings.xml "
                "(ActiveSettings.cpp:242-268 at 2.6)"
            ),
        ),
    )


# The 3DS console-identity levels of the emulated SD tree — compile-time
# all-zero strings in the emulator (ID0 the system identifier hash, ID1 the
# scrambled SD CID; archive.h:22-24 at Azahar 2125.1.1), so the container
# below any SD root is one fixed spelling.
_AZAHAR_ZERO_ID = "00000000000000000000000000000000"
_AZAHAR_CONTAINER = os.path.join("Nintendo 3DS", _AZAHAR_ZERO_ID, _AZAHAR_ZERO_ID)


def _azahar_setting(
    values: Mapping[tuple[str, str], str], section: str, key: str, default: str
) -> tuple[str, bool]:
    """One setting read the way the emulator reads it → (value, configured).

    ``<key>\\default=true`` makes the compiled default win over any stored
    value (ReadSetting, config.cpp:1442-1450 at 2125.1.1); an absent key is
    the default too. ``configured`` says whether the stored value governed.
    """
    if values.get((section, f"{key}\\default"), "").casefold() == "true":
        return default, False
    stored = values.get((section, key))
    if stored is None or stored == "":
        return default, False
    return stored, True


def _azahar_virtual_sd_caveat(
    values: Mapping[tuple[str, str], str], card: StandaloneSaveCard
) -> Caveat | None:
    """The one statement ``use_virtual_sd = false`` earns — no SD, said so."""
    virtual_sd, _ = _azahar_setting(values, "Data Storage", "use_virtual_sd", "true")
    if virtual_sd.casefold() == "true":
        return None
    return Caveat(
        CAVEAT_CORE_MODE_UNESTABLISHED,
        "use_virtual_sd is switched off — no SD card is emulated, so whether and where "
        "a game's save lands is not established; the tree below is where the "
        "configuration would put it",
        {"core": card.token, "reason": "use_virtual_sd = false disables the emulated SD"},
    )


def _azahar_sdmc_root(
    values: Mapping[tuple[str, str], str],
    stated_ini: str | None,
    *,
    sandbox: _Sandbox,
    card: StandaloneSaveCard,
    ini_path: str,
) -> tuple[str | None, tuple[OptionReading, ...], Unresolved | None]:
    """(configured SD root, the readings that decided it, the refusal if any).

    ``None`` for the root means the compiled default governs: custom storage
    off, or on with an empty path (UpdateUserPath returns on empty). A
    configured path only the emulator's sandbox could read refuses the whole
    question — nothing here could anchor an answer — and the refusal rides
    the third slot so every return carries one shape.
    """
    section = "Data Storage"
    custom, custom_configured = _azahar_setting(values, section, "use_custom_storage", "false")
    readings = [
        _reading_with_file(
            OptionReading(
                "use_custom_storage",
                custom if custom_configured else None,
                (
                    f'qt-config.ini: [Data Storage] use_custom_storage = "{custom}"'
                    if custom_configured
                    else "use_custom_storage is unset or marked default — the compiled default "
                    "false governs (settings.h:485 at 2125.1.1)"
                ),
                None,
            ),
            stated_ini,
        )
    ]
    if custom.casefold() != "true":
        return None, tuple(readings), None
    configured_dir, dir_configured = _azahar_setting(values, section, "sdmc_directory", "")
    readings.append(
        _reading_with_file(
            OptionReading(
                "sdmc_directory",
                configured_dir if dir_configured else None,
                (
                    f'qt-config.ini: [Data Storage] sdmc_directory = "{configured_dir}"'
                    if dir_configured
                    else "sdmc_directory names nothing — an empty custom path leaves the "
                    "default standing (UpdateUserPath returns on empty)"
                ),
                None,
            ),
            stated_ini,
        )
    )
    if not dir_configured:
        return None, tuple(readings), None
    resolved = sandbox.host("sdmc_directory", configured_dir)
    if resolved.path is None:
        refusal = Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
            f"the SD path Azahar's configuration names ({configured_dir!r}) has no "
            f"spelling on this host — {ini_path} read fine, and nothing this answer "
            "could anchor at",
            {"emulator": card.token, "config": ini_path, "path": configured_dir},
        )
        return None, tuple(readings), refusal
    return resolved.path, tuple(readings), None


def _azahar_savefile_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavefilePlacement | Unresolved:
    """Azahar's save answer: the per-title unit on the emulated SD.

    The SD root comes from ``qt-config.ini`` exactly as the emulator resolves
    it — ``use_custom_storage`` routes ``sdmc_directory``, false leaves the
    XDG default (``<data>/azahar-emu/sdmc/``) — and the unit below it is
    ``Nintendo 3DS/<ID0>/<ID1>/title/<save_id>/data/00000001/``, the title id
    as two lowercase 8-hex segments (archive_source_sd_savedata.cpp:21-29).
    The extdata tree beside it is stated as its own group: save-adjacent data
    keyed by an id of its own, which no title id fills.
    """
    ini_path = _standalone_settings_path(card, homes)
    result = machine.read_text(ini_path)
    if result.status not in (READ_OK, READ_MISSING):
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"Azahar's configuration ({ini_path}) exists and could not be read — where the "
            "emulated SD and every save on it live is unknowable here",
            {"emulator": card.token, "config": ini_path},
        )
    values = qt_ini.values(result.text) if result.status == READ_OK and result.text else {}
    caveats: list[Caveat] = [*extra_caveats]
    stated_ini = ini_path if result.status == READ_OK else None
    virtual_sd_caveat = _azahar_virtual_sd_caveat(values, card)
    if virtual_sd_caveat is not None:
        caveats.append(virtual_sd_caveat)
    sdmc_root, readings, refusal = _azahar_sdmc_root(
        values, stated_ini, sandbox=sandbox, card=card, ini_path=ini_path
    )
    if refusal is not None:
        return refusal
    if sdmc_root is None:
        sdmc_root = os.path.join(homes.emulator_root(XDG_DATA, card.token), "sdmc")
    container = os.path.join(sdmc_root, _AZAHAR_CONTAINER)
    title_tree = os.path.join(container, "title")
    physical_tree, link_caveats = _link_view(machine, title_tree)
    caveats.extend(link_caveats)
    directory = os.path.join(title_tree, TEMPLATE_SAVE_ID, "data", "00000001")
    physical = (
        os.path.join(physical_tree, TEMPLATE_SAVE_ID, "data", "00000001")
        if physical_tree is not None
        else None
    )
    extdata = os.path.join(container, "extdata")
    caveats.append(
        Caveat(
            CAVEAT_FILE_NAMES_UNESTABLISHED,
            "the files below a title's save directory are the game's own writes — move the "
            "directory whole; fill <save_id> with the title id, high word then low word, each "
            "8 lowercase hex digits, as two path segments",
            {
                "core": card.token,
                "dir": directory,
                "role": ROLE_BATTERY,
                "save_id": "the 3DS title id: <high 8 hex>/<low 8 hex>, lowercase",
                "citation": "archive_source_sd_savedata.cpp:21-29 at Azahar 2125.1.1 — "
                "'{}Nintendo 3DS/{}/{}/title/' + '{:08x}/{:08x}/data/00000001/'; "
                "ID0/ID1 are all-zero (archive.h:22-24)",
            },
        )
    )
    caveats.append(
        Caveat(
            CAVEAT_FILE_NAMES_UNESTABLISHED,
            "extdata beside the title tree keeps save-adjacent data per game, keyed by the "
            "title's own extdata id — an id the title id does not fill — so the tree is stated "
            "and its entries refused; back it up whole to be safe",
            {
                "core": card.token,
                "dir": extdata,
                "role": ROLE_BATTERY,
                "citation": "archive_extsavedata.cpp at Azahar 2125.1.1 — "
                "'{}Nintendo 3DS/{}/{}/extdata/'",
            },
        )
    )
    return SavefilePlacement(
        dir=directory,
        root_kind=ROOT_EMULATOR_DIRECTORY,
        needs=(HOLE_SAVE_ID,),
        fallback_dir=None,
        file_set=FileSet(
            FILE_SET_DECLARED,
            (),
            f"declared by standalone save card '{card.token}'",
            complete=False,
            groups=(
                FileGroup(
                    dir=directory,
                    files=None,
                    granularity=GRANULARITY_PER_GAME_DIRECTORY,
                    role=ROLE_BATTERY,
                ),
                FileGroup(
                    dir=extdata,
                    files=None,
                    granularity=GRANULARITY_PER_GAME_FILES,
                    role=ROLE_BATTERY,
                ),
            ),
        ),
        sources=(f"standalone save card '{card.token}': {card.provenance}",),
        caveats=tuple(caveats),
        physical_dir=physical,
        granularity=Granularity(
            value=GRANULARITY_PER_GAME_DIRECTORY,
            mode="sdmc",
            readings=tuple(readings),
            alternatives=(),
            provenance=(
                f"standalone save card '{card.token}': the emulated SD from qt-config.ini "
                "(config.cpp:480-498 at 2125.1.1)"
            ),
        ),
    )


# DuckStation's slot vocabulary, in the emulator's own declaration order
# (settings.cpp:1743-1745 at stenzek/duckstation@64655818e — the last commit
# before RetroDECK's 2024-09-19 "Legacy" freeze). Matching is case-insensitive
# the way ParseMemoryCardTypeName matches, and an unparseable value falls back
# to the compiled default the way ``.value_or`` does (settings.cpp:391-398).
_DUCKSTATION_TYPE_NAMES = (
    "None",
    "Shared",
    "PerGame",
    "PerGameTitle",
    "PerGameFileTitle",
    "NonPersistent",
)
_DUCKSTATION_TYPE_DEFAULTS = ("PerGameTitle", "None")  # settings.h:510-511


def _duckstation_sanitized(name: str) -> str:
    """``Path::SanitizeFileName``'s Linux branch: ``/`` and ``*`` become ``_``.

    ``FileSystemCharacterIsSane`` splits on the platform
    (file_system.cpp:69-97 at 64655818e). The ``#else`` arm the shipped Linux
    build compiles rejects exactly two characters: ``/`` when slashes are
    being stripped (:82-83, and ``SanitizeFileName`` defaults ``strip_slashes``
    to true) and ``*`` (:86-87, "drop asterisks too, they make globbing
    annoying"). ``:`` is inside a further ``#ifdef __APPLE__`` (:90-92) and
    the control bytes, the angle brackets and the rest belong to the
    ``_WIN32`` arm above them (:71-80) — this mirror used to replace control
    bytes too, which is Windows behaviour on a Linux build. The replacement
    character is ``_`` (:152).
    """
    return "".join("_" if ch in "/*" else ch for ch in name)


def _duckstation_settings(
    machine: Machine,
    homes: _XdgHomes,
    card: "StandaloneSaveCard | StandaloneSavestateCard",
    *,
    lost: str = "which cards its slots hold and where they live is unknowable here",
) -> tuple[str, dict[tuple[str, str], str], str | None, tuple[Caveat, ...], Unresolved | None]:
    """The DataRoot probe: (root, settings values, stated ini, caveats, refusal).

    DuckStation's DataRoot is picked by the launch environment —
    ``$XDG_CONFIG_HOME/duckstation`` where that variable is set and absolute,
    else ``~/.local/share/duckstation`` (qthost.cpp:562-582) — and
    ``settings.ini`` lives inside it. No file records the environment, so the
    probe reads both spellings in that order and the file that exists speaks;
    where neither does, the ambiguity is stated and the compiled defaults
    hang off the environment-unset side. The savestate question probes the
    same pair for its own key, which is why the sentence a refusal carries is
    a parameter.
    """
    read = duckstation.read_settings(
        machine,
        config_home=homes.base("config"),
        data_home=homes.base("data"),
        flatpak=homes.flatpak,
        xdg_pinned=homes.xdg_pinned,
    )
    if read.unreadable is not None:
        refusal = Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"DuckStation's configuration ({read.unreadable}) exists and could not be read — {lost}",
            {"emulator": card.token, "config": read.unreadable},
        )
        return read.root, {}, None, (), refusal
    if not read.ambiguous:
        return read.root, dict(read.values), read.stated_path, (), None
    ambiguity = duckstation.dataroot_caveat(card.token, "the compiled defaults below")
    return read.root, {}, None, (ambiguity,), None


@dataclass(frozen=True, slots=True)
class _DuckSlot:
    """One memory-card slot's contribution to the answer."""

    mode: str
    group: FileGroup | None = None
    readings: tuple[OptionReading, ...] = ()
    caveats: tuple[Caveat, ...] = ()


def _duckstation_shared_slot(
    slot: int,
    values: Mapping[tuple[str, str], str],
    memcards_dir: str,
    sandbox: _Sandbox,
    type_reading: OptionReading,
) -> _DuckSlot:
    """The shared card: ``CardXPath`` — absolute or Directory-relative — else the default name.

    The three arms are ``GetSharedMemoryCardPath``'s own (settings.cpp:1785-1797
    at 64655818e): an empty value combines the default name below the
    memory-card directory (:1790), a relative one combines there too (:1792),
    and only a value upstream's ``Path::IsAbsolute`` accepts stands alone — no
    ``RealPath`` follows any of them, unlike the folder reader. Both combines
    are :func:`atlas.qt_ini.path_combine`, not ``os.path.join``, because the
    two disagree on degenerate spellings (#325). For a configured name the
    degenerate spelling is the value's own: ``cards//alpha.mcd/`` names the
    file the emulator opens instead of a directory-shaped spelling of it. For
    the default name it is the *base*'s: an absolute ``[MemoryCards]
    Directory`` reaches every slot as spelled, and the combine collapsing it
    is what keeps a default-named slot in the same group directory as a
    configured sibling — under ``os.path.join`` the two slots spelled one
    directory two ways, and the answer's file list, which aggregates only the
    groups sharing the first group's directory, silently dropped the second
    card.
    """
    n = slot + 1
    found_path, path_spelled = _simpleini_value(values, "MemoryCards", f"Card{n}Path")
    raw_path = found_path or ""
    path_reading = OptionReading(
        f"Card{n}Path",
        raw_path or None,
        (
            f'settings.ini: [MemoryCards] {path_spelled} = "{raw_path}"'
            if raw_path
            else f"Card{n}Path is unset — the default shared_card_{n}.mcd below the memory-card "
            "directory governs (settings.cpp:1785-1797)"
        ),
        None,
    )
    if not raw_path:
        resolved = qt_ini.path_combine(memcards_dir, f"shared_card_{n}.mcd")
    elif not os.path.isabs(raw_path):
        resolved = qt_ini.path_combine(memcards_dir, raw_path)
    else:
        host = sandbox.host(f"Card{n}Path", raw_path)
        if host.path is None:
            return _DuckSlot(
                mode="Shared",
                readings=(type_reading, path_reading),
                caveats=(
                    Caveat(
                        CAVEAT_SANDBOX_PATH_UNTRANSLATED,
                        f"settings.ini sets Card{n}Path to {raw_path!r}, a path only the "
                        "emulator's sandbox can read — the shared card could not be located "
                        "from here",
                        {"key": f"Card{n}Path", "path": raw_path},
                    ),
                ),
            )
        resolved = host.path
    return _DuckSlot(
        mode="Shared",
        group=FileGroup(
            dir=os.path.dirname(resolved),
            files=(os.path.basename(resolved),),
            granularity=GRANULARITY_SHARED_CARD,
            role="memory-card",
        ),
        readings=(type_reading, path_reading),
    )


def _duckstation_per_game_slot(
    slot: int,
    mode: str,
    values: Mapping[tuple[str, str], str],
    memcards_dir: str,
    card: StandaloneSaveCard,
    type_reading: OptionReading,
    content_path: str | None,
) -> _DuckSlot:
    """The three per-game modes: one ``<name>_<slot>.mcd`` below the memory-card directory."""
    n = slot + 1
    shared_fallback = f"shared_card_{n}.mcd"
    readings: list[OptionReading] = [type_reading]
    if mode == "PerGameFileTitle":
        stem = (
            _duckstation_sanitized(os.path.splitext(os.path.basename(content_path))[0])
            if content_path is not None
            else None
        )
        name = f"{stem}_{n}.mcd" if stem is not None else f"{TEMPLATE_ROM_STEM}_{n}.mcd"
        fill = (
            "the content file's own name without its extension, sanitized the way "
            "Path::SanitizeFileName sanitizes on Linux ('/' and '*' become '_')"
        )
        citation = "system.cpp:3744-3762 and file_system.cpp:142-163 at 64655818e"
    elif mode == "PerGame":
        name = f"{TEMPLATE_SAVE_ID}_{n}.mcd"
        fill = "the disc's serial, as DuckStation reads it off the running game"
        citation = "system.cpp:3663-3685 and settings.cpp:1799-1802 at 64655818e"
    else:
        name = f"{TEMPLATE_SAVE_ID}_{n}.mcd"
        raw_playlist, playlist_spelled = _simpleini_value(values, "MemoryCards", "UsePlaylistTitle")
        readings.append(
            OptionReading(
                "UsePlaylistTitle",
                raw_playlist,
                (
                    f'settings.ini: [MemoryCards] {playlist_spelled} = "{raw_playlist}"'
                    if raw_playlist is not None
                    else "UsePlaylistTitle is unset — the default true governs "
                    "(settings.cpp:401)"
                ),
                None,
            )
        )
        fill = (
            "the game's title as DuckStation's database spells it, sanitized — a playlist or "
            "disc-set game may use the set's name instead, and an existing disc-title card "
            "outranks it"
        )
        citation = "system.cpp:3688-3742 and settings.cpp:1799-1802 at 64655818e"
    caveat = Caveat(
        CAVEAT_FILENAMES_CONTENT_CONDITIONAL,
        f"slot {n} names its card by {fill}; a running game without that fact falls back to "
        f"the shared card ({shared_fallback})",
        {
            "core": card.token,
            "mode": mode,
            "files": name,
            "files_without_save_id": shared_fallback,
            "save_id": fill,
            "citation": citation,
        },
    )
    return _DuckSlot(
        mode=mode,
        group=FileGroup(
            dir=memcards_dir,
            files=(name,),
            granularity=GRANULARITY_PER_GAME_FILE,
            role="memory-card",
        ),
        readings=tuple(readings),
        caveats=(caveat,),
    )


def _duckstation_slot(
    slot: int,
    values: Mapping[tuple[str, str], str],
    memcards_dir: str,
    sandbox: _Sandbox,
    card: StandaloneSaveCard,
    content_path: str | None,
) -> _DuckSlot:
    """One slot read the way the emulator reads it: the type first, then its paths."""
    n = slot + 1
    raw, type_spelled = _simpleini_value(values, "MemoryCards", f"Card{n}Type")
    parsed = next(
        (
            t
            for t in _DUCKSTATION_TYPE_NAMES
            if raw is not None and t.casefold() == raw.strip().casefold()
        ),
        None,
    )
    mode = parsed if parsed is not None else _DUCKSTATION_TYPE_DEFAULTS[slot]
    if parsed is not None:
        provenance = f'settings.ini: [MemoryCards] {type_spelled} = "{raw}"'
    elif raw is not None:
        provenance = (
            f'settings.ini sets {type_spelled} to "{raw}", a value ParseMemoryCardTypeName '
            f"does not know — the compiled default {mode} governs (.value_or, "
            "settings.cpp:391-398)"
        )
    else:
        provenance = (
            f"Card{n}Type is unset — the compiled default {mode} governs (settings.h:510-511)"
        )
    type_reading = OptionReading(f"Card{n}Type", raw, provenance, None)
    if mode == "None":
        return _DuckSlot(mode=mode, readings=(type_reading,))
    if mode == "NonPersistent":
        return _DuckSlot(
            mode=mode,
            readings=(type_reading,),
            caveats=(
                Caveat(
                    CAVEAT_SAVE_WRITES_DISCARDED,
                    f"slot {n} holds a non-persistent card — writes into it are discarded at "
                    "shutdown and nothing is kept (MemoryCardType::NonPersistent)",
                    {"core": card.token, "mode": f"Card{n}Type = NonPersistent"},
                ),
            ),
        )
    if mode == "Shared":
        return _duckstation_shared_slot(slot, values, memcards_dir, sandbox, type_reading)
    return _duckstation_per_game_slot(
        slot, mode, values, memcards_dir, card, type_reading, content_path
    )


def _first_directory_files(groups: tuple[FileGroup, ...]) -> tuple[str, ...]:
    """Every established name in the first group's directory — the FileSet invariant.

    A multi-slot emulator can put two cards side by side in one directory, and
    :class:`~atlas.placement.FileSet` requires ``files`` to be all of them, in
    order. Taking only the first group's names is what raises instead of
    answering, so both slot-pair resolvers compose the flat list here.
    """
    if not groups or groups[0].files is None:
        return ()
    directory = groups[0].dir
    return tuple(
        name for group in groups if group.dir == directory and group.files for name in group.files
    )


def _duckstation_needs(groups: tuple[FileGroup, ...]) -> tuple[str, ...]:
    """The holes the slot templates still carry, in first-appearance order."""
    holes: list[str] = []
    for group in groups:
        for name in group.files or ():
            if TEMPLATE_SAVE_ID in name:
                holes.append(HOLE_SAVE_ID)
            if TEMPLATE_ROM_STEM in name:
                holes.append(HOLE_ROM_STEM)
    return tuple(dict.fromkeys(holes))


# The section-qualified keys DuckStation's save answer depends on, and what a
# per-game value does to them. All five, unconditionally: a game ini can change
# the slot MODES themselves, so even a machine whose slots are both ``None``
# today can be answered differently for one game (settings.cpp:391-401).
_DUCKSTATION_SAVE_LAYER_KEYS = (
    "[MemoryCards] Card1Type",
    "[MemoryCards] Card2Type",
    "[MemoryCards] Card1Path",
    "[MemoryCards] Card2Path",
    "[MemoryCards] UsePlaylistTitle",
)
# The asymmetry is the useful half: the card FILE can leave the directory, the
# directory itself cannot move.
_DUCKSTATION_SAVE_LAYER_GOVERNS = (
    "A per-game value there decides which card each slot holds and which file it reads, and an "
    "absolute CardNPath is used verbatim rather than joined below the memory-card directory "
    "(GetSharedMemoryCardPath, settings.cpp:1785-1797), so a card named there need not sit in "
    "the directory this answer states. The directory itself is fixed: [MemoryCards] Directory "
    "is read from the base settings alone (EmuFolders::LoadConfig, settings.cpp:1964-1981), "
    "and no per-game file moves it."
)
# The door those five come through: Settings::Load is handed the LAYERED
# interface, because Host::GetSettingsInterface returns the layered object
# itself rather than its base layer.
_DUCKSTATION_SAVE_LAYER_READ = "Settings::Load, settings.cpp:391-401, on host.cpp:42-45"


def _duckstation_game_settings_caveats(
    machine: Machine,
    values: Mapping[tuple[str, str], str],
    data_root: str,
    token: str,
    *,
    sandbox: _Sandbox,
    keys: tuple[str, ...],
    governs: str,
    read_through: str,
) -> list[Caveat]:
    """The per-game layer where this machine loads one — DuckStation's second settings source.

    The directory resolution and the sandbox hop; the words and the listing
    live in :mod:`atlas.duckstation`, because the firmware route states the
    same fact about its own keys and the two must say it identically. An
    absolute configured value is translated through the launch's sandbox like
    every other path this configuration names, and one with no host spelling
    is the unread state rather than the silent one — the listing this caveat
    rests on cannot be made from here.
    """
    if not duckstation.applies_game_settings(values):
        return []
    raw, _ = _simpleini_value(
        values, duckstation.GAME_SETTINGS_SECTION, duckstation.GAME_SETTINGS_KEY
    )
    if raw and os.path.isabs(raw):
        host = sandbox.host(duckstation.GAME_SETTINGS_KEY, raw)
        if host.path is None:
            # The cause rides beside the consequence, the way every other
            # stands-around-it site emits it: a client sees WHY the layer went
            # unread, and that the stated directory is the configured sandbox
            # spelling rather than a host directory that failed to glob.
            return [
                *host.caveats,
                duckstation.per_game_unread_caveat(
                    token=token,
                    directory=raw,
                    keys=keys,
                    governs=governs,
                    read_through=read_through,
                    sandbox_value=raw,
                ),
            ]
        directory = host.path
    else:
        directory = duckstation.load_path(
            values,
            data_root,
            duckstation.GAME_SETTINGS_SECTION,
            duckstation.GAME_SETTINGS_KEY,
            duckstation.GAME_SETTINGS_DEFAULT,
        )
    return duckstation.per_game_caveats(
        machine,
        token=token,
        directory=directory,
        keys=keys,
        governs=governs,
        read_through=read_through,
    )


def _duckstation_savefile_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavefilePlacement | Unresolved:
    """DuckStation's save answer: two memory-card slots, six modes each.

    The Dolphin GC shape, spoken in DuckStation's vocabulary: each slot's
    ``CardXType`` picks its scheme, the mode pair rides ``granularity.mode``,
    and every group is a place a card really lands — a per-game
    ``<name>_<slot>.mcd`` below the memory-card directory, or a shared card
    at its configured or default path.
    """
    data_root, values, stated_ini, probe_caveats, refusal = _duckstation_settings(
        machine, homes, card
    )
    if refusal is not None:
        return refusal
    caveats: list[Caveat] = [*extra_caveats, *probe_caveats]
    caveats.extend(
        _duckstation_game_settings_caveats(
            machine,
            values,
            data_root,
            card.token,
            sandbox=sandbox,
            keys=_DUCKSTATION_SAVE_LAYER_KEYS,
            governs=_DUCKSTATION_SAVE_LAYER_GOVERNS,
            read_through=_DUCKSTATION_SAVE_LAYER_READ,
        )
    )
    found_dir, dir_spelled = _simpleini_value(values, "MemoryCards", "Directory")
    raw_dir = found_dir or ""
    dir_reading = OptionReading(
        "Directory",
        raw_dir or None,
        (
            f'settings.ini: [MemoryCards] {dir_spelled} = "{raw_dir}"'
            if raw_dir
            else "Directory is unset — the default memcards below the DataRoot governs "
            "(settings.cpp:1943, :1974)"
        ),
        None,
    )
    # The directory is a LoadPathFromSettings read (EmuFolders::LoadConfig,
    # settings.cpp:1974 with :1952-1962 at 64655818e): unset and empty alike
    # fall to the compiled default, and a relative value combines below the
    # DataRoot — through the emulator's own combine, whose degenerate-spelling
    # behaviour and RealPath tail :func:`atlas.duckstation.load_path` states.
    if not raw_dir:
        memcards_dir = os.path.join(data_root, "memcards")
    elif not os.path.isabs(raw_dir):
        memcards_dir = qt_ini.path_combine(data_root, raw_dir)
    else:
        host = sandbox.host("Directory", raw_dir)
        if host.path is None:
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
                f"the memory-card directory DuckStation's configuration names ({raw_dir!r}) "
                f"has no spelling on this host — {stated_ini} read fine, and nothing this "
                "answer could anchor at",
                {"emulator": card.token, "config": stated_ini or "", "path": raw_dir},
            )
        memcards_dir = host.path
    slots = tuple(
        _duckstation_slot(slot, values, memcards_dir, sandbox, card, content_path)
        for slot in (0, 1)
    )
    groups = tuple(slot.group for slot in slots if slot.group is not None)
    readings = [dir_reading]
    for slot in slots:
        readings.extend(slot.readings)
    caveats.extend(c for slot in slots for c in slot.caveats)
    mode = "+".join(slot.mode for slot in slots)
    if groups:
        directory = groups[0].dir
        files = _first_directory_files(groups)
        needs = _duckstation_needs(groups)
    else:
        directory = memcards_dir
        files = ()
        needs = ()
        caveats.append(
            Caveat(
                CAVEAT_SAVE_WRITES_DISCARDED,
                "no slot keeps a card (Card1Type/Card2Type) — a game finds nowhere to save "
                "and nothing is kept; the granularity block names the switches that would "
                "change that",
                {"core": card.token, "mode": mode},
            )
        )
    # The answer's own directory, which is the memory-card one only while no
    # slot points elsewhere: a slot with an absolute CardXPath moves `dir`, and
    # `physical_dir` is a statement about `dir` (a dead link on the directory
    # the answer names is what makes writes fail).
    physical, link_caveats = _link_view(machine, directory)
    caveats.extend(link_caveats)
    return SavefilePlacement(
        dir=directory,
        root_kind=ROOT_EMULATOR_DIRECTORY,
        needs=needs,
        fallback_dir=None,
        file_set=FileSet(
            FILE_SET_DECLARED,
            files,
            f"declared by standalone save card '{card.token}'",
            complete=False,
            groups=groups,
        ),
        sources=(f"standalone save card '{card.token}': {card.provenance}",),
        caveats=tuple(caveats),
        physical_dir=physical,
        granularity=Granularity(
            value=groups[0].granularity if groups else GRANULARITY_NONE,
            mode=mode,
            readings=tuple(_reading_with_file(r, stated_ini) for r in readings),
            alternatives=(),
            provenance=(
                f"standalone save card '{card.token}': the slot pair from settings.ini "
                "(settings.cpp:391-401 at 64655818e)"
            ),
        ),
    )


# PCSX2's eight slot spellings, in the emulator's own order: the two console
# ports, then the six multitap slots (Pcsx2Config.cpp:2035-2047 at v2.6.3).
# Each entry is (enable key, filename key, default filename, enabled default,
# multitap port) — the multitap slots default off, and `port` is the [Pad]
# switch they ALSO depend on (#315), None for the two console slots, which
# depend on nothing but their own enable.
_PCSX2_SLOTS = tuple(
    [
        ("Slot1_Enable", "Slot1_Filename", "Mcd001.ps2", True, None),
        ("Slot2_Enable", "Slot2_Filename", "Mcd002.ps2", True, None),
    ]
    + [
        (
            f"Multitap{port}_Slot{slot}_Enable",
            f"Multitap{port}_Slot{slot}_Filename",
            f"Mcd-Multitap{port}-Slot{slot:02d}.ps2",
            False,
            port,
        )
        for port in (1, 2)
        for slot in (2, 3, 4)
    ]
)

# The multitap's own switch, which is NOT in [MemoryCards] and is not spelled
# the way the C++ member is (#315). `SettingsWrapSection("Pad")` then
# `SettingsWrapBitBoolEx(MultitapPort0_Enabled, "MultitapPort1")` and
# `(MultitapPort1_Enabled, "MultitapPort2")` (Pcsx2Config.cpp:1815-1817 at
# v2.6.3): the ini key is one-based where the member is zero-based, so the
# key that pairs with the `Multitap1_Slot*` card spellings is `MultitapPort1`.
# Both spellings being one-based, the pairing is by number.
#
# The compiled default is off, and reachably so: the default handed to
# GetBoolValue is the member's CURRENT value (SettingsWrapper.h:133,
# SettingsWrapper.cpp:96-99), which would be a stale-value trap were it not
# for VMManager::ApplySettings doing `EmuConfig = Pcsx2Config()` at :726
# immediately before LoadSettings() at :728 — and PadOptions' constructor ends
# in `bitset = 0` (Pcsx2Config.cpp:1779-1788). Pad::SetDefaultControllerConfig
# writes the same false into a fresh ini (SIO/Pad/Pad.cpp:172-173).
_PCSX2_MULTITAP_SECTION = "Pad"
_PCSX2_MULTITAP_PORT_KEYS = {1: "MultitapPort1", 2: "MultitapPort2"}

# The section-qualified keys the save answer depends on, in the emulator's own
# order — derived from the tables above so the statement and the reading cannot
# drift apart. All sixteen memory-card keys deliberately: a per-game file can
# turn on a multitap slot this machine keeps off, so "this answer names two
# cards" is itself something such a file overturns, and naming only the four
# console-port keys would understate the layer's reach. The two [Pad] keys join
# them because the slot listing now depends on them too (#315) and they come
# through the same layered read — `Pcsx2Config::LoadSave` is `LoadSaveCore`
# AND `Pad.LoadSave` (Pcsx2Config.cpp:2028-2033) — so listing only sixteen
# would understate that reach in the other direction.
_PCSX2_MEMCARD_LAYER_KEYS = tuple(
    f"[MemoryCards] {key}" for slot in _PCSX2_SLOTS for key in (slot[0], slot[1])
) + tuple(
    f"[{_PCSX2_MULTITAP_SECTION}] {key}" for key in _PCSX2_MULTITAP_PORT_KEYS.values()
)

_PCSX2_MEMCARD_READ_THROUGH = (
    "Pcsx2Config::LoadSaveMemcards, Pcsx2Config.cpp:2035-2054, and "
    "Pcsx2Config::PadOptions::LoadSave, :1790-1818 — both reached from "
    f"{_PCSX2_LOAD_CORE} through Pcsx2Config::LoadSave (:2028-2033)"
)

# The precision this answer needs. PCSX2's layer moves a card's FILE NAME inside
# a directory it cannot move, and an absolute name does not escape either: the
# name is joined onto the memory-card directory by Path::Combine, which appends
# one separator and then swallows the leading separator of what follows
# (PathAppendString, FileSystem.cpp:98-139, entered with last_separator true).
# Where PCSX2 wants an absolute value to win it tests for one — LoadPathFromSettings
# does exactly that at Pcsx2Config.cpp:2275 — and FullpathToMcd does not.
_PCSX2_SAVE_GOVERNS = (
    "A per-game file can name a different card file for a slot, and can hold a card in a slot "
    "this machine keeps empty. It cannot move the directory those names are read in: "
    "FullpathToMcd joins the name onto the memory-card directory (Pcsx2Config.cpp:2065-2068) "
    "and Path::Combine concatenates rather than letting the name replace it — even an "
    "absolute name lands below that directory, because the separator it opens with is "
    "swallowed (FileSystem.cpp:847-862, PathAppendString :98-139) — while [Folders] "
    f"MemoryCards is a folder setting, and {_PCSX2_FOLDERS_ARE_BASE_ONLY}. What such a name "
    "does decide beside the file is the card's kind: the type is read off whatever sits at "
    "the composed path, so a per-game name can make a slot a folder card where this answer "
    "states a file card (FileMcd_SetType, MemoryCardFile.cpp:584-604). A multitap slot takes "
    "one more switch than its own enable — [Pad] MultitapPort1/MultitapPort2, read through the "
    "same layered call (Pcsx2Config.cpp:2028-2033, :1815-1817) — so such a file can also turn a "
    "whole multitap on or off under an unchanged [MemoryCards] section."
)


class _Pcsx2Tap(NamedTuple):
    """One multitap port's switch: the value the emulator would see, and its reading."""

    enabled: bool
    reading: OptionReading


def _pcsx2_multitap_ports(
    values: Mapping[tuple[str, str], str],
) -> dict[int, _Pcsx2Tap]:
    """``[Pad] MultitapPort{1,2}``, read the way the emulator reads them (#315).

    A memory-card slot behind a multitap needs this switch as well as its own
    ``[MemoryCards]`` enable, and the two are read through the same layered
    call — ``Pcsx2Config::LoadSave`` is ``LoadSaveCore`` *and* ``Pad.LoadSave``
    (Pcsx2Config.cpp:2028-2033). The value is parsed with the emulator's own
    ``FromChars<bool>``, so a value that is neither true nor false leaves the
    compiled default standing exactly as it does for the slot enables
    (INISettingsInterface.cpp:198-210).
    """
    ports: dict[int, _Pcsx2Tap] = {}
    for port, key in _PCSX2_MULTITAP_PORT_KEYS.items():
        raw, spelled = _simpleini_value(values, _PCSX2_MULTITAP_SECTION, key)
        parsed = qt_ini.from_chars_bool(raw)
        if parsed is not None:
            provenance = f'PCSX2.ini: [{_PCSX2_MULTITAP_SECTION}] {spelled} = "{raw}"'
        elif raw is not None:
            provenance = (
                f'PCSX2.ini sets {spelled} to "{raw}", which FromChars<bool> reads as neither '
                "true nor false — the default false governs "
                "(INISettingsInterface.cpp:198-210 at v2.6.3)"
            )
        else:
            provenance = (
                f"{key} is unset — the default false governs (PadOptions' constructor ends in "
                "bitset = 0, Pcsx2Config.cpp:1779-1788, and ApplySettings resets the config "
                "before every load, VMManager.cpp:726-728)"
            )
        ports[port] = _Pcsx2Tap(
            enabled=parsed if parsed is not None else False,
            reading=OptionReading(key, raw, provenance, None),
        )
    return ports


def _pcsx2_slot_group(
    machine: Machine,
    values: Mapping[tuple[str, str], str],
    slot: tuple[str, str, str, bool, int | None],
    memcards_dir: str,
    card: StandaloneSaveCard,
    *,
    multitap_enabled: bool | None,
) -> tuple[str, FileGroup | None, tuple[OptionReading, ...], tuple[Caveat, ...]]:
    """One slot: (type word, group, readings, caveats) — the type read off the disk.

    ``FileMcd_SetType`` (MemoryCardFile.cpp:584-604): an empty filename
    empties the slot; a directory at the card's full path makes it a folder
    card — per-game saves as auto-managed subdirectories inside — and
    anything else is a file card, one shared image that would be created on
    first use.

    The full path is :func:`atlas.qt_ini.path_combine` of the memory-card directory
    and the name, which is why this takes no sandbox: the configured value is a
    file NAME and cannot carry a root of its own past that join (#312).

    *multitap_enabled* is the ``[Pad]`` switch for this slot's port, or ``None``
    for a console slot that has none. A multitap slot needs BOTH switches (#315)
    and answers ``"tap-off"`` when its own enable is on and the tap is not: the
    word is distinct from ``"off"`` on purpose, because "configured and the
    emulator would not open it" is a different fact from "never configured",
    and the caller keeps the enable's reading for the first while dropping it
    for the second.
    """
    enable_key, filename_key, default_name, enabled_default, _port = slot
    raw_enable, enable_spelled = _simpleini_value(values, "MemoryCards", enable_key)
    parsed_enable = qt_ini.from_chars_bool(raw_enable)
    enabled = parsed_enable if parsed_enable is not None else enabled_default
    default_word = "true" if enabled_default else "false"
    if parsed_enable is not None:
        enable_provenance = f'PCSX2.ini: [MemoryCards] {enable_spelled} = "{raw_enable}"'
    elif raw_enable is not None:
        enable_provenance = (
            f'PCSX2.ini sets {enable_spelled} to "{raw_enable}", which FromChars<bool> reads as '
            f"neither true nor false — the default {default_word} governs "
            "(INISettingsInterface.cpp:198-210 at v2.6.3)"
        )
    else:
        enable_provenance = f"{enable_key} is unset — the default {default_word} governs"
    readings = [OptionReading(enable_key, raw_enable, enable_provenance, None)]
    if not enabled:
        return "off", None, tuple(readings), ()
    if multitap_enabled is False:
        # The card is configured and the emulator opens none for the running
        # game: with the tap off, MultitapProtocol::Select refuses to move
        # currentMemcardSlot off 0 and SupportCheck answers "absent"
        # (MultitapProtocol.cpp:17-38, :41-60), so Sio2::Memcard only ever
        # reaches slot 0 of each port (Sio2.cpp:247). FileMemoryCard::Open
        # skips the slot outright (MemoryCardFile.cpp:271-277); a FOLDER card
        # there is still opened and its directory even created
        # (FolderMemoryCardAggregator::Open, MemoryCardFolder.cpp:2291-2297,
        # which carries no such guard) — but nothing the game can address, so
        # this answer names no save location either way.
        return "tap-off", None, tuple(readings), ()
    raw_name, name_spelled = _simpleini_value(values, "MemoryCards", filename_key)
    name = raw_name if raw_name is not None else default_name
    readings.append(
        OptionReading(
            filename_key,
            raw_name,
            (
                f'PCSX2.ini: [MemoryCards] {name_spelled} = "{raw_name}"'
                if raw_name is not None
                else f"{filename_key} is unset — the default {default_name} governs "
                "(MemoryCardFile.cpp:244-250)"
            ),
            None,
        )
    )
    if name == "":
        return "empty", None, tuple(readings), ()
    # The name is a name, not a path — even when it is spelled like one. It is
    # composed the way the emulator composes it (#312), and that join never
    # lets the value replace the directory: see :func:`atlas.qt_ini.path_combine`.
    # Nothing here goes through the sandbox, and that is the fix rather than an
    # omission — the memory-card DIRECTORY was already translated by
    # :func:`_pcsx2_memcards_dir`, and an absolute name contributes no root of
    # its own for a second translation to be about.
    full = qt_ini.path_combine(memcards_dir, name)
    if machine.path_kind(full) == KIND_DIRECTORY:
        caveat = Caveat(
            CAVEAT_FILE_NAMES_UNESTABLISHED,
            f"the card at {os.path.basename(full)} is a folder card — each game's saves live "
            "as subdirectories inside, auto-managed by the emulator — so the tree is stated "
            "and its entries refused; back it up whole",
            {
                "core": card.token,
                "dir": full,
                "role": "memory-card",
                "citation": "FileMcd_SetType, MemoryCardFile.cpp:584-604 at v2.6.3 — a "
                "directory at the card's full path is MemoryCardType::Folder "
                "(McdFolderAutoManage default true, Pcsx2Config.cpp:1922)",
            },
        )
        group = FileGroup(
            dir=full, files=None, granularity=GRANULARITY_PER_GAME_FILES, role="memory-card"
        )
        return "folder", group, tuple(readings), (caveat,)
    group = FileGroup(
        dir=os.path.dirname(full),
        files=(os.path.basename(full),),
        granularity=GRANULARITY_SHARED_CARD,
        role="memory-card",
    )
    return "file", group, tuple(readings), ()


def _pcsx2_memcards_dir(
    values: Mapping[tuple[str, str], str],
    data_root: str,
    *,
    sandbox: _Sandbox,
    card: StandaloneSaveCard,
    ini_path: str,
) -> tuple[str | None, OptionReading, Unresolved | None]:
    """(memory-card directory, its reading, the refusal if any) — one shape per return."""
    raw_dir, dir_spelled = _simpleini_value(values, "Folders", "MemoryCards")
    resolved, provenance = _pcsx2_folder_below_dataroot(
        raw_dir,
        key="MemoryCards",
        default="memcards",
        default_citation="Pcsx2Config.cpp:2259",
        data_root=data_root,
        spelled=dir_spelled,
    )
    reading = OptionReading("MemoryCards", raw_dir, provenance, None)
    if resolved is not None:
        return resolved, reading, None
    assert raw_dir is not None  # only an absolute value leaves the helper unresolved
    host = sandbox.host("MemoryCards", raw_dir)
    if host.path is None:
        refusal = Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
            f"the memory-card directory PCSX2's configuration names ({raw_dir!r}) has no "
            f"spelling on this host — {ini_path} read fine, and nothing this answer "
            "could anchor at",
            {"emulator": card.token, "config": ini_path, "path": raw_dir},
        )
        return None, reading, refusal
    return host.path, reading, None


class _Pcsx2Listing(NamedTuple):
    """What PCSX2's eight slot spellings come to: the answer's listing, and why."""

    mode: str
    groups: tuple[FileGroup, ...]
    readings: tuple[OptionReading, ...]
    caveats: tuple[Caveat, ...]


def _pcsx2_slot_listing(
    machine: Machine,
    values: Mapping[tuple[str, str], str],
    memcards_dir: str,
    card: StandaloneSaveCard,
    taps: Mapping[int, _Pcsx2Tap],
) -> _Pcsx2Listing:
    """The eight slot spellings, resolved into what this answer lists.

    Each slot lands in exactly one of three states, and they are three
    different facts rather than degrees of one:

    * **absent** — a multitap slot disabled in ``[MemoryCards]``. Dropped
      whole: six of them would drown the answer in noise, and a slot nobody
      configured is not news.
    * **suppressed** (``"tap-off"``) — enabled there while its ``[Pad]``
      multitap switch is off, so the emulator opens nothing and no save of the
      running game reaches it (#315). It joins neither the mode string nor the
      file set, but **its enable reading travels**, which is the whole of what
      lets a caller tell it from an absent one.
    * **listed** — everything else: its word joins the mode string, its group
      the file set, its caveats the answer.

    A port's ``[Pad]`` reading joins whenever that port had a say — it either
    let a slot in or kept one out — once per port and in port order. They are
    appended after the slot readings rather than interleaved, which is what
    keeps the reading list byte-identical on every machine with no multitap
    slot enabled.
    """
    modes: list[str] = []
    groups: list[FileGroup] = []
    readings: list[OptionReading] = []
    caveats: list[Caveat] = []
    tap_readings: dict[int, OptionReading] = {}
    for slot in _PCSX2_SLOTS:
        port = slot[4]
        word, group, slot_readings, slot_caveats = _pcsx2_slot_group(
            machine,
            values,
            slot,
            memcards_dir,
            card,
            multitap_enabled=None if port is None else taps[port].enabled,
        )
        if port is not None:
            if word == "off":
                continue  # absent
            tap_readings.setdefault(port, taps[port].reading)
        readings.extend(slot_readings)
        if word == "tap-off":
            continue  # suppressed — the readings above are all it contributes
        modes.append(word)
        caveats.extend(slot_caveats)
        if group is not None:
            groups.append(group)
    readings.extend(tap_readings[tap_port] for tap_port in sorted(tap_readings))
    return _Pcsx2Listing(
        mode="+".join(modes),
        groups=tuple(groups),
        readings=tuple(readings),
        caveats=tuple(caveats),
    )


def _pcsx2_savefile_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavefilePlacement | Unresolved:
    """PCSX2's save answer: up to eight card slots, each file or folder.

    One DataRoot spelling on Linux — the config side whether or not
    ``XDG_CONFIG_HOME`` is set (Pcsx2Config.cpp:2197-2217) — with
    ``inis/PCSX2.ini`` inside. The type of every enabled card is read off the
    disk the way ``FileMcd_SetType`` reads it, so a folder card answers as
    the per-game tree it is and a file card as the shared image it is.
    """
    settings = _standalone_settings(card)
    data_root = homes.emulator_root(settings.bases[0], card.token)
    ini_path = settings.only(
        config_home=homes.base("config"), data_home=homes.base("data"), flatpak=homes.flatpak
    )
    result = machine.read_text(ini_path)
    if result.status not in (READ_OK, READ_MISSING):
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"PCSX2's configuration ({ini_path}) exists and could not be read — which cards "
            "its slots hold and where they live is unknowable here",
            {"emulator": card.token, "config": ini_path},
        )
    values = qt_ini.values(result.text) if result.status == READ_OK and result.text else {}
    stated_ini = ini_path if result.status == READ_OK else None
    caveats: list[Caveat] = [*extra_caveats]
    memcards_dir, dir_reading, refusal = _pcsx2_memcards_dir(
        values, data_root, sandbox=sandbox, card=card, ini_path=ini_path
    )
    if refusal is not None:
        return refusal
    assert memcards_dir is not None  # the helper refuses whenever it cannot name one
    listing = _pcsx2_slot_listing(
        machine, values, memcards_dir, card, _pcsx2_multitap_ports(values)
    )
    caveats.extend(listing.caveats)
    mode = listing.mode
    groups = listing.groups
    readings = (dir_reading, *listing.readings)
    if groups:
        directory = groups[0].dir
        files = _first_directory_files(groups)
    else:
        directory = memcards_dir
        files = ()
        caveats.append(
            Caveat(
                CAVEAT_SAVE_WRITES_DISCARDED,
                "no slot holds a card (SlotN_Enable / an empty SlotN_Filename) — a game finds "
                "nowhere to save and nothing is kept; the granularity block names the switches "
                "that would change that",
                {"core": card.token, "mode": mode},
            )
        )
    # The answer's own directory — `physical_dir` speaks for `dir`.
    #
    # `dir` cannot leave the memory-card directory by way of a slot's FILENAME,
    # and that is the emulator's rule rather than a convenience of this code
    # (#312): every card's full path is
    # ``Path::Combine(EmuFolders::MemoryCards, Filename)`` (``FullpathToMcd``,
    # Pcsx2Config.cpp:2065-2068), a combine with no ``IsAbsolute`` test, so an
    # absolute name lands BELOW that directory — see
    # :func:`atlas.qt_ini.path_combine`. What CAN move `dir` is a name carrying a
    # sub-path (``sub/card.ps2`` puts the group one level down), which is the
    # same thing the emulator would open.
    physical, link_caveats = _link_view(machine, directory)
    caveats.extend(link_caveats)
    caveats.extend(
        _pcsx2_game_settings_caveats(
            machine,
            values,
            data_root,
            card.token,
            sandbox=sandbox,
            keys=_PCSX2_MEMCARD_LAYER_KEYS,
            governs=_PCSX2_SAVE_GOVERNS,
            read_through=_PCSX2_MEMCARD_READ_THROUGH,
        )
    )
    return SavefilePlacement(
        dir=directory,
        root_kind=ROOT_EMULATOR_DIRECTORY,
        needs=(),
        fallback_dir=None,
        file_set=FileSet(
            FILE_SET_DECLARED,
            files,
            f"declared by standalone save card '{card.token}'",
            complete=False,
            groups=groups,
        ),
        sources=(f"standalone save card '{card.token}': {card.provenance}",),
        caveats=tuple(caveats),
        physical_dir=physical,
        granularity=Granularity(
            value=groups[0].granularity if groups else GRANULARITY_NONE,
            mode=mode,
            readings=tuple(_reading_with_file(r, stated_ini) for r in readings),
            alternatives=(),
            provenance=(
                f"standalone save card '{card.token}': the slot pair from PCSX2.ini "
                "(Pcsx2Config.cpp:2035-2047 at v2.6.3), each card's type read off the disk "
                "(FileMcd_SetType)"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# melonDS 1.1 — one .sav per game, in the directory [Instance0] SaveFilePath
# names, read the way Config::Load reads it: melonDS.toml where it exists —
# even unparseable, because the emulator catches the syntax error and runs on
# factory defaults rather than falling back — and the pre-1.0 melonDS.ini
# line by line only where no TOML exists (Config.cpp:682-803 at 1.1). The
# empty default puts the save beside the ROM itself (getAssetPath,
# EmuInstance.cpp:445-484).
# ---------------------------------------------------------------------------

# The archive suffixes melonDS's frontend recognizes, matched
# case-insensitively against the file name's end (Window.cpp:124-148 at 1.1).
# Content inside one names its save after the archived file — a name the
# archive's own path does not derive — so the <rom_stem> hole stays open.
_MELONDS_ARCHIVE_SUFFIXES = (
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".tar.bz2",
    ".tbz2",
    ".tar.lz4",
    ".tlz4",
    ".tar.zst",
    ".tzst",
    ".tar.z",
    ".taz",
    ".tar.lz",
    ".tar.lzma",
    ".tlz",
    ".tar.lrz",
    ".tlrz",
    ".tar.lzo",
    ".tzo",
)


def _melonds_is_archive(content_path: str) -> bool:
    """Whether melonDS's frontend would open this content as an archive."""
    return os.path.basename(content_path).casefold().endswith(_MELONDS_ARCHIVE_SUFFIXES)


def _melonds_stem(content_name: str) -> str:
    """The loaded file's name minus its last extension (EmuInstance.cpp:1884).

    A name that is all extension leaves the base empty, and getAssetPath then
    writes ``firmware`` in its place (:473-476) — mirrored, not repaired.
    """
    dot = content_name.rfind(".")
    stem = content_name[:dot] if dot != -1 else content_name
    return stem or "firmware"


@dataclass(frozen=True, slots=True)
class _MelonConfig:
    """What the config read established: the raw value, and its own story."""

    raw: str | None
    provenance: str
    stated_file: str | None


def _melonds_save_path_provenance(
    config: melonds.MelonConfig, raw: str | None, *, key: str = "SaveFilePath", what: str = "save"
) -> str:
    """The sentence the path reading carries, per Load()'s branch.

    *key* and *what* let the savestate question speak the same chain about its
    own row — ``SavestatePath`` is ``SaveFilePath``'s sibling in the legacy
    table (Config.cpp:302 beside :301) and every cited line here is the shared
    machinery both keys go through.
    """
    if config.source == melonds.SOURCE_TOML_INVALID:
        return (
            "melonDS.toml exists and is not parseable TOML — melonDS catches the syntax "
            "error and runs on factory defaults (Config.cpp:796-803), so the empty default "
            f"governs and the {what} lands beside the ROM"
        )
    if config.source == melonds.SOURCE_TOML:
        if raw:
            return f'melonDS.toml: [Instance0] {key} = "{raw}"'
        if isinstance(melonds.raw_value(config, f"Instance0.{key}"), str):
            return (
                f'melonDS.toml: [Instance0] {key} = "" — the empty value routes the '
                f"{what} beside the ROM (getAssetPath, EmuInstance.cpp:448-449)"
            )
        return (
            f"melonDS.toml states no [Instance0] {key} (unset, or not a string, reads "
            f"as the empty default — Config.cpp:596) — the {what} lands beside the ROM"
        )
    if config.source == melonds.SOURCE_LEGACY:
        if raw:
            return (
                "melonDS.toml is absent, so the pre-1.0 melonDS.ini speaks until the first "
                f"launch migrates it (Config.cpp:785-795): {key}={raw}"
            )
        return (
            "melonDS.toml is absent and the pre-1.0 melonDS.ini it falls back to states no "
            f"{key} — the empty default governs and the {what} lands beside the ROM"
        )
    return (
        "neither melonDS.toml nor the pre-1.0 melonDS.ini exists — the compiled "
        f"defaults govern and the {what} lands beside the ROM (getAssetPath, "
        "EmuInstance.cpp:445-484)"
    )


def _melonds_config(
    machine: Machine,
    homes: _XdgHomes,
    card: "StandaloneSaveCard | StandaloneSavestateCard",
    *,
    key: str = "SaveFilePath",
    what: str = "save",
    lost: str = "where every game's .sav lands is unknowable here",
) -> _MelonConfig | Unresolved:
    """``[Instance0] <key>``, read the way Config::Load reads it.

    The read chain lives in :mod:`atlas.melonds`, shared with the firmware
    route — the TOML speaks wherever it exists, only a TOML that does not
    exist reaches the legacy INI beside it, and where neither exists the
    compiled defaults govern (Config.cpp:785-803 at 1.1). The save and
    savestate questions ask it about sibling rows of one table, which is why
    the key is a parameter rather than a second copy of the chain.
    """
    read = melonds.read_config(machine, homes.base("config"), homes.flatpak)
    if read.unreadable is not None:
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"melonDS's configuration ({read.unreadable}) exists and could not be read — {lost}",
            {"emulator": card.token, "config": read.unreadable},
        )
    config = read.config
    assert config is not None  # a read is either a document or an unreadable path
    value = melonds.get_string(config, f"Instance0.{key}")
    raw = value or None
    return _MelonConfig(
        raw=raw,
        provenance=_melonds_save_path_provenance(config, raw, key=key, what=what),
        stated_file=config.stated_file,
    )


@dataclass(frozen=True, slots=True)
class _MelonRoot:
    """Where the save directory anchors, resolved the way getAssetPath composes it."""

    directory: str
    root_kind: RootKind
    mode: str
    needs: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()
    refusal: Unresolved | None = None


def _melonds_root(
    config: _MelonConfig,
    card: "StandaloneSaveCard | StandaloneSavestateCard",
    sandbox: _Sandbox,
    content_path: str | None,
    *,
    key: str = "SaveFilePath",
    what: str = "save",
) -> _MelonRoot:
    """The three places a melonDS asset can anchor: the configured directory, the ROM's own, the cwd.

    getAssetPath uses the configured value only after trimming its trailing
    separators (EmuInstance.cpp:459-467), so a value of only separators falls
    to the working directory the way any relative value does — the composed
    path is opened verbatim by the process, a property of the launch. The
    savestate question walks the identical composition for its own key
    (getSavestateName hands SavestatePath to the same function,
    EmuInstance.cpp:696-701), which is why the key is a parameter.
    """
    if not config.raw:
        if content_path is not None:
            return _MelonRoot(
                directory=os.path.dirname(content_path),
                root_kind=ROOT_CONTENT_DIRECTORY,
                mode="rom-dir",
            )
        return _MelonRoot(
            directory=TEMPLATE_CONTENT_DIR,
            root_kind=ROOT_CONTENT_DIRECTORY,
            mode="rom-dir",
            needs=(HOLE_CONTENT_DIR,),
        )
    trimmed = config.raw.rstrip("/\\")
    if not os.path.isabs(trimmed):
        return _MelonRoot(
            directory=os.path.join(TEMPLATE_CWD, trimmed) if trimmed else TEMPLATE_CWD,
            root_kind=ROOT_WORKING_DIRECTORY,
            mode="cwd-relative",
            needs=(HOLE_CWD,),
            caveats=(
                Caveat(
                    CAVEAT_SAVE_DIR_LAUNCH_DEPENDENT,
                    f"{key} is the relative value {config.raw!r}, which melonDS opens "
                    "relative to the working directory of the launching process (getAssetPath "
                    "composes it verbatim, EmuInstance.cpp:445-484) — a property of the "
                    "launch, not of the machine; fill 'cwd' with the launcher's working "
                    "directory to complete the path",
                    {"core": card.token},
                ),
            ),
        )
    host = sandbox.host(key, trimmed)
    if host.path is None:
        refusal = Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
            f"the {what} directory melonDS's configuration names ({trimmed!r}) has no "
            f"spelling on this host — {config.stated_file} read fine, and nothing this "
            "answer could anchor at",
            {"emulator": card.token, "config": config.stated_file or "", "path": trimmed},
        )
        return _MelonRoot(
            directory="", root_kind=ROOT_EMULATOR_DIRECTORY, mode="", refusal=refusal
        )
    # ``mode`` is the savefile granularity's word alone — the savestate answer
    # carries no granularity, so its caller never reads it.
    return _MelonRoot(
        directory=host.path, root_kind=ROOT_EMULATOR_DIRECTORY, mode="save-file-path"
    )


def _melonds_files(
    card: StandaloneSaveCard, content_path: str | None
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[Caveat, ...]]:
    """The one save name: the content's stem where it derives, the open hole where not."""
    if content_path is not None and not _melonds_is_archive(content_path):
        return (f"{_melonds_stem(os.path.basename(content_path))}.sav",), (), ()
    if content_path is not None:
        sentence = (
            "the content is an archive — melonDS names the save after the file inside it "
            "(EmuInstance.cpp:1846-1848), which the archive's own path does not derive; "
            "fill <rom_stem> with the archived file's name without its last extension"
        )
    else:
        sentence = (
            "the save is named after the loaded file — its name without the last extension, "
            "and for a ROM inside an archive the name of the file inside it "
            "(EmuInstance.cpp:1884, :1846-1848); fill <rom_stem> with that name"
        )
    caveat = Caveat(
        CAVEAT_FILENAMES_CONTENT_CONDITIONAL,
        sentence,
        {
            "core": card.token,
            "files": f"{TEMPLATE_ROM_STEM}.sav",
            "rom_stem": "the loaded file's name without its last extension — for an "
            "archive, the archived file's",
            "citation": "EmuInstance.cpp:1884, :1846-1848 at 1.1",
        },
    )
    return (f"{TEMPLATE_ROM_STEM}.sav",), (HOLE_ROM_STEM,), (caveat,)


def _melonds_savefile_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavefilePlacement | Unresolved:
    """melonDS's save answer: one ``<rom stem>.sav`` where SaveFilePath points.

    No slots and no modes — the whole answer is one directory and one name.
    The directory is the configured one, the ROM's own where the value is
    empty, or the launching process's working directory where it is relative;
    the name is the content's, filled where the content is named and a plain
    file, held open as ``<rom_stem>`` where it is an archive or unnamed.
    The multi-instance suffix stays out: instance 0, the launch the catalogue
    performs, appends nothing (EmuInstance.cpp:176-181, :1891).
    """
    config = _melonds_config(machine, homes, card)
    if isinstance(config, Unresolved):
        return config
    root = _melonds_root(config, card, sandbox, content_path)
    if root.refusal is not None:
        return root.refusal
    files, name_needs, name_caveats = _melonds_files(card, content_path)
    caveats: list[Caveat] = [*extra_caveats, *root.caveats, *name_caveats]
    if root.directory.startswith("<"):
        physical = None
    else:
        physical, link_caveats = _link_view(machine, root.directory)
        caveats.extend(link_caveats)
    reading = OptionReading("SaveFilePath", config.raw, config.provenance, None)
    return SavefilePlacement(
        dir=root.directory,
        root_kind=root.root_kind,
        needs=(*root.needs, *name_needs),
        fallback_dir=None,
        file_set=FileSet(
            FILE_SET_DECLARED,
            files,
            f"declared by standalone save card '{card.token}'",
            complete=False,
            groups=(
                FileGroup(
                    dir=root.directory,
                    files=files,
                    granularity=GRANULARITY_PER_GAME_FILE,
                    role=ROLE_BATTERY,
                ),
            ),
        ),
        sources=(f"standalone save card '{card.token}': {card.provenance}",),
        caveats=tuple(caveats),
        physical_dir=physical,
        granularity=Granularity(
            value=GRANULARITY_PER_GAME_FILE,
            mode=root.mode,
            readings=(_reading_with_file(reading, config.stated_file),),
            alternatives=(),
            provenance=(
                f"standalone save card '{card.token}': SaveFilePath from melonDS.toml, the "
                "pre-1.0 melonDS.ini it migrates, or the compiled default (Config::Load, "
                "Config.cpp:785-803 at 1.1)"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# RPCS3 build 7c6b3dcd — the save tree hangs off the emulated PS3's internal
# drive, whose host directory vfs.yml states (/dev_hdd0/, defaulting to
# $(EmulatorDir)dev_hdd0/, vfs_config.h:13). Below it, one directory per title
# id under home/<user>/savedata; the user is a runtime selection nothing on
# disk records, so every user home that exists is stated as its own tree.
# ---------------------------------------------------------------------------

_RPCS3_EMULATOR_DIR_KEY = "$(EmulatorDir)"
_RPCS3_HDD0_KEY = "/dev_hdd0/"
_RPCS3_HDD0_DEFAULT = "$(EmulatorDir)dev_hdd0/"
# The user home the emulator starts with (Emulator::m_usr, System.h:164), used
# where no home directory can be listed — never as a claim that it is the one
# in force, which the caveat states.
_RPCS3_FIRST_USER = "00000001"
# The virtual memory cards for PS1 and PS2 classics, outside the per-user tree
# — a whole string in the shipped binary. Stated, not walked: what lands there
# and under which names has not been read.
_RPCS3_VMC_SUBDIR = os.path.join("savedata", "vmc")


@dataclass(frozen=True, slots=True)
class _PerUserSaves:
    """The shape two emulators share: one save tree per user account.

    RPCS3 keeps saves at ``dev_hdd0/home/<user>/savedata`` and Vita3K at
    ``ux0/user/<user>/savedata``. Both pick the user at run time; RPCS3 writes
    the selection down nowhere, Vita3K records it in config.yml — so both
    state *every* user directory that exists, and the assembly is theirs
    jointly. What differs is the words and the citations, which each card
    supplies.

    ``user_root`` is the directory holding the user directories,
    ``first_user`` the one the emulator starts with — used only where nothing
    could be listed, never as a claim that it is the one running.

    ``user_reason`` is the machine-readable half of ``user_sentence``: *why*
    the running user is or is not settled here. The two emulators do not share
    it, because they do not share the fact — RPCS3 writes the selection down
    nowhere, while Vita3K's configuration records a user id whose effect
    depends on how the launch was made. ``configured_user`` carries that
    recorded id where one exists, so a client reads the emulator's own
    preselection instead of only the directory listing.

    ``headline_user`` names the user whose tree the answer's ``dir`` points
    at, where the caller established one: Vita3K sets it to the recorded user
    exactly when the listing found that user's directory, because a frontend
    launch reopens exactly that user then — and the caller resolves it
    together with the sentence that explains it, so the two cannot drift.
    RPCS3 never sets it: no file records its user, so its headline stays the
    first tree found.
    """

    user_root: str
    first_user: str
    names_citation: str
    user_sentence: str
    # What to say when the tree holds no user directory at all. The other
    # sentence claims every user found here is stated, which reads as a survey
    # when none was found and the answer is standing on the compiled default
    # alone — a directory the emulator would create, not one seen on this
    # machine.
    no_user_sentence: str
    user_reason: str
    mode: str
    readings: tuple[OptionReading, ...]
    reading_file: str | None
    provenance: str
    configured_user: str | None = None
    headline_user: str | None = None


def _per_user_state(
    shape: _PerUserSaves,
    card: StandaloneSaveCard,
    users: tuple[str, ...],
    listing: GlobResult,
) -> tuple[str, dict[str, str]]:
    """What the listing established about the users — three states, not two.

    "Users were found" and "no user exists here" are the two an emptied result
    reads as, and a listing that *failed* is neither: it establishes nothing at
    all, and answering it with the empty tree's sentence claims contents the
    failed listing never reached. That is the defect this round fixed for
    DuckStation's BIOS directory, so it does not get to live on here.

    ``users`` names the trees this answer points at, which is what it has
    always named — where none were found, the compiled default stands in and
    the sentence says so in as many words.
    """
    if listing.status != GLOB_COMPLETE:
        sentence = (
            f"{shape.user_root} could not be listed, so which users exist below it is not "
            "established — the tree named is the one the emulator starts with, and whether "
            "this machine has that user, others, or none is unknown here"
        )
        reason = "which users exist here was not established"
    elif not users:
        sentence, reason = shape.no_user_sentence, "no user directory was found"
    else:
        sentence, reason = shape.user_sentence, shape.user_reason
    data = {
        "core": card.token,
        "reason": reason,
        "users": ",".join(users) if users else shape.first_user,
    }
    # The recorded user is a reading of the configuration, not of the tree, so
    # it holds whatever the listing did or did not establish.
    if shape.configured_user is not None:
        data["configured_user"] = shape.configured_user
    return sentence, data


def _per_user_listing(machine: Machine, user_root: str) -> tuple[GlobResult, tuple[str, ...]]:
    """The user directories below ``user_root``, and the listing that found them.

    Hoisted out of the placement so an emulator whose words depend on what the
    listing found — Vita3K's recorded user — resolves them before building its
    shape, on the same observation the placement then states. A user is a
    directory: anything else the glob hands back — a stray file beside the
    user homes, a dead link — would otherwise become a group naming
    ``<that file>/savedata``, a path nothing writes to.
    """
    listing = machine.glob(os.path.join(user_root, "*"))
    users = tuple(
        sorted(
            os.path.basename(path)
            for path in listing.matches
            if machine.path_kind(path) == KIND_DIRECTORY
        )
    )
    return listing, users


def _per_user_savedata_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    shape: _PerUserSaves,
    listing: GlobResult,
    users: tuple[str, ...],
    extra_caveats: tuple[Caveat, ...],
    extra_groups: tuple[FileGroup, ...] = (),
    trailing_caveats: tuple[Caveat, ...] = (),
) -> SavefilePlacement:
    """One group per user account, each a per-game-directory tree.

    ``listing`` and ``users`` come from :func:`_per_user_listing` on
    ``shape.user_root`` — passed in rather than taken here so the caller and
    this assembly state the same observation.

    ``extra_caveats`` lead and ``trailing_caveats`` follow the two this shape
    always states, which is the order each emulator's answer already had.
    ``extra_groups`` are places beside the per-user trees that belong to the
    same save — RPCS3's virtual memory cards — and they follow the user groups
    so the headline never lands on them.
    """
    groups = tuple(
        FileGroup(
            dir=os.path.join(shape.user_root, user, "savedata"),
            files=None,
            granularity=GRANULARITY_PER_GAME_DIRECTORY,
            role=ROLE_BATTERY,
        )
        # Where nothing was found the compiled default stands in — as the tree
        # the emulator starts with, never as a user seen on this machine, which
        # is what the caveat below has to say in so many words.
        for user in (users or (shape.first_user,))
    ) + extra_groups
    if shape.headline_user is not None:
        # The caller resolved this identity against the emulator's own user
        # listing. The join composes the tree that identity writes — usually a
        # group's own tree, but a user.xml answering to another id names a
        # tree the first save creates, so the headline is not read off groups.
        directory = os.path.join(shape.user_root, shape.headline_user, "savedata")
    else:
        directory = groups[0].dir
    caveats: list[Caveat] = [
        *extra_caveats,
        Caveat(
            CAVEAT_FILE_NAMES_UNESTABLISHED,
            "each directory below savedata is one title's own, named by its title id and "
            "written by the game — move a directory whole rather than its files",
            {
                "core": card.token,
                "dir": directory,
                "role": ROLE_BATTERY,
                "citation": shape.names_citation,
            },
        ),
        Caveat(CAVEAT_CORE_MODE_UNESTABLISHED, *_per_user_state(shape, card, users, listing)),
        *trailing_caveats,
    ]
    if listing.status != GLOB_COMPLETE:
        # Structured, not an appended clause: "which users exist here is
        # unknown" is a degradation a client branches on, and prose is not
        # something a client can branch on. ``path`` is the key the code's
        # other emitter uses and the guide documents, so a client that
        # branches on the code and reads it finds the directory here too.
        caveats.append(
            Caveat(
                CAVEAT_SAVE_DIR_UNLISTABLE,
                f"{shape.user_root} could not be listed, so which user directories are under "
                "it is unknown — the tree below is what the compiled default names, not what "
                "was found",
                {"path": shape.user_root, "core": card.token},
            )
        )
    physical, link_caveats = _link_view(machine, directory)
    caveats.extend(link_caveats)
    return SavefilePlacement(
        dir=directory,
        root_kind=ROOT_EMULATOR_DIRECTORY,
        needs=(),
        fallback_dir=None,
        file_set=FileSet(
            FILE_SET_DECLARED,
            (),
            f"declared by standalone save card '{card.token}'",
            complete=False,
            groups=groups,
        ),
        sources=(f"standalone save card '{card.token}': {card.provenance}",),
        caveats=tuple(caveats),
        physical_dir=physical,
        granularity=Granularity(
            value=GRANULARITY_PER_GAME_DIRECTORY,
            mode=shape.mode,
            readings=tuple(
                _reading_with_file(reading, shape.reading_file) for reading in shape.readings
            ),
            alternatives=(),
            provenance=shape.provenance,
        ),
    )


def _rpcs3_savefile_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavefilePlacement | Unresolved:
    """RPCS3's save answer: the drive vfs.yml names, then one directory per title.

    Two steps, both the emulator's own. ``cfg_vfs::get`` takes the configured
    ``/dev_hdd0/`` or its compiled default, replaces ``$(EmulatorDir)``
    everywhere — an empty one meaning the config directory — and appends a
    separator (vfs_config.cpp:14-62). Below the drive the tree is
    ``home/<user>/savedata``, one directory per title id.

    The user is where this answer stops short of certainty: it is a runtime
    selection (``m_usr``, System.h:164) and no file records which one is in
    force, so every user home that exists becomes a group and the caveat says
    the running emulator uses one of them.
    """
    settings = _standalone_settings(card)
    config_dir = homes.emulator_root(settings.bases[0], card.token)
    vfs_path = settings.only(
        config_home=homes.base("config"), data_home=homes.base("data"), flatpak=homes.flatpak
    )
    result = machine.read_text(vfs_path)
    if result.status not in (READ_OK, READ_MISSING):
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"RPCS3's VFS configuration ({vfs_path}) exists and could not be read — which "
            "drive its saves live on is unknowable here",
            {"emulator": card.token, "config": vfs_path},
        )
    text = result.text or "" if result.status == READ_OK else ""
    read = read_scalars(text, fallbacks={_RPCS3_EMULATOR_DIR_KEY: f"{config_dir}/"})
    if read.refusal is not None:
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"RPCS3's VFS configuration ({vfs_path}) states a construct atlas does not read "
            f"({read.refusal}) — which drive its saves live on is unknowable here",
            {"emulator": card.token, "config": vfs_path, "reason": read.refusal},
        )
    if _RPCS3_HDD0_KEY in read.skipped:
        # Stated as a nested block, a list or a multi-line scalar: RPCS3 reads a
        # drive here and atlas did not. Treating that as an unset key answered
        # the compiled default and said "the compiled default governs" — a
        # provenance line about a key the file does set.
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"RPCS3's VFS configuration ({vfs_path}) states {_RPCS3_HDD0_KEY} as a construct "
            "atlas does not read — its value is unread, not absent, so which drive its saves "
            "live on is unknowable here",
            {
                "emulator": card.token,
                "config": vfs_path,
                "reason": f"{_RPCS3_HDD0_KEY} is unread",
            },
        )
    stated = read.get(_RPCS3_HDD0_KEY)
    if stated:
        provenance = f'vfs.yml: {_RPCS3_HDD0_KEY} = "{stated}"'
    else:
        provenance = (
            f"{_RPCS3_HDD0_KEY} is unset — the compiled default "
            f"{_RPCS3_HDD0_DEFAULT} governs (vfs_config.h:13)"
        )
    raw = stated or f"{config_dir}/dev_hdd0/"
    host = sandbox.host(_RPCS3_HDD0_KEY, raw)
    if host.path is None:
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
            f"the drive RPCS3's VFS configuration names ({raw!r}) has no spelling on "
            f"this host — {vfs_path} read fine, and nothing this answer could anchor at",
            {"emulator": card.token, "config": vfs_path, "path": raw},
        )
    hdd0 = host.path
    vmc = os.path.join(hdd0, _RPCS3_VMC_SUBDIR)
    user_root = os.path.join(hdd0, "home")
    listing, users = _per_user_listing(machine, user_root)
    return _per_user_savedata_placement(
        machine,
        card=card,
        listing=listing,
        users=users,
        shape=_PerUserSaves(
            user_root=user_root,
            first_user=_RPCS3_FIRST_USER,
            names_citation=(
                "'/dev_hdd0/home/%08u/savedata/' as a whole string in the shipped binary "
                "(build 7c6b3dcd)"
            ),
            user_sentence=(
                "which user account the emulator runs as is a runtime selection — it starts "
                f"at {_RPCS3_FIRST_USER} (Emulator::m_usr, System.h:164) and the user manager "
                "changes it — and no file records the current one, so every user home found "
                "here is stated"
            ),
            no_user_sentence=(
                f"no user home exists below {user_root} — nothing has saved "
                f"here yet. The tree named is the one the emulator starts with, user "
                f"{_RPCS3_FIRST_USER} (Emulator::m_usr, System.h:164), which it would create "
                "on the first save; it is not a home found on this machine"
            ),
            user_reason="the active user account is not recorded on disk",
            mode="hdd0",
            readings=(OptionReading(_RPCS3_HDD0_KEY, stated, provenance, None),),
            reading_file=vfs_path if result.status == READ_OK else None,
            provenance=(
                f"standalone save card '{card.token}': the drive from vfs.yml "
                "(cfg_vfs::get, vfs_config.cpp:14-62 at build 7c6b3dcd)"
            ),
        ),
        extra_caveats=extra_caveats,
        # The virtual memory cards are a directory beside the per-user tree,
        # not an image the answer names as a file — so they ride as a group of
        # their own with their names left unestablished, and the caveat that
        # goes with that shape is the one for a part of the save beyond this
        # answer's root. ``save-inside-image`` said the opposite of what is
        # true here: that nothing inside is addressable.
        extra_groups=(
            FileGroup(
                dir=vmc,
                files=None,
                granularity=GRANULARITY_PER_GAME_FILE,
                role=ROLE_MEMORY_CARD,
            ),
        ),
        trailing_caveats=(
            Caveat(
                CAVEAT_FILE_SET_SPANS_ROOTS,
                "PS1 and PS2 classics save onto virtual memory cards outside the per-user "
                f"tree, at {vmc} — a sync that walks only the per-user savedata tree misses "
                "them. It is a directory, and what lands in it under which names has not "
                "been read, so the place is stated and its contents are not; it is in "
                "file_set.groups with its names left open",
                {
                    "core": card.token,
                    "mode": "hdd0",
                    "dir": vmc,
                    "files": "",
                },
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Vita3K build 3996 (commit cb1f592c) — one configuration key carries the whole
# tree. ``pref-path`` in config.yml, and everything the emulator keeps hangs
# off it as ``ux0/…``; saves are ``ux0/user/<user id>/savedata/<title id>``
# (io.cpp:136-143). Same user-account shape as RPCS3, and the same answer to
# it: every user directory that exists is stated.
# ---------------------------------------------------------------------------

_VITA3K_PREF_PATH_KEY = "pref-path"
# The two keys that record a user preselection. ``user-id`` is the id the GUI
# writes when a user is opened (select_and_open_user, user_management.cpp:329-331)
# and ``user-auto-connect`` is the switch that opens it without asking. Both
# only matter through init_home (gui.cpp:688-696) — see the caveat sentence.
_VITA3K_USER_ID_KEY = "user-id"
_VITA3K_AUTO_CONNECT_KEY = "user-auto-connect"
_VITA3K_USER_TREE = os.path.join("ux0", "user")
# The user the emulator's own redirect comment names (io.cpp:203), used where
# no user directory can be listed — never as a claim that it is the one in use.
_VITA3K_FIRST_USER = "00"


# What one user directory's user.xml made of it, read the way get_users_list
# reads it. "listed" is the only fate that yields an identity; the two
# no-user.xml fates are the emulator's own skip (load_file fails the same way
# for a missing file and a malformed one), and "unreadable" is atlas's alone —
# a file the emulator may well load, whose contents this answer cannot know.
_VITA3K_USER_LISTED = "listed"
_VITA3K_USER_NO_XML = "no-user-xml"
_VITA3K_USER_XML_INVALID = "does-not-parse"
_VITA3K_USER_XML_UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class _Vita3kListedUser:
    """One user directory as get_users_list would take it.

    ``identity`` is the gui.users key the directory yields — the user.xml's
    ``id`` attribute where its root ``<user>`` element carries one (present
    but empty counts as present, matching pugixml's attribute test), and the
    directory name's stem otherwise — or ``None`` when the directory yields
    no user at all, or when whether it does could not be read.
    """

    directory: str
    identity: str | None
    fate: str


def _vita3k_stem(name: str) -> str:
    """``std::filesystem::path::stem`` of a name, spelled the way libstdc++ cuts.

    The extension begins at the rightmost period unless it leads the name or
    the name is ``.`` or ``..`` (``_M_find_extension``) — so ``01.bak`` stems
    to ``01`` and ``..bak`` to ``.``. Neither stdlib spelling is that mirror:
    ``os.path.splitext`` skips a leading run of periods and ``PurePath.stem``
    keeps a trailing one.
    """
    if name in (".", ".."):
        return name
    i = name.rfind(".")
    return name[:i] if i > 0 else name


def _vita3k_listed_user(machine: Machine, user_root: str, user: str) -> _Vita3kListedUser:
    """One user directory's fate — the classification get_users_list performs.

    The directory joins ``gui.users`` only when its ``user.xml`` loads
    (get_users_list, user_management.cpp:89 at cb1f592c), keyed by the file's
    ``id`` attribute or, lacking one, the directory name's stem —
    ``path.stem()`` (:94-97). A missing user.xml and one that does not parse
    are one fact to the emulator — ``load_file`` fails and the directory is
    skipped — and that is mirrored here. A user.xml atlas could not read is
    neither: the emulator may load it, so the fate says so and the caller
    refuses to decide. The file is local trusted configuration, parsed with
    stdlib etree the way ``atlas/esde.py`` documents.
    """
    result = machine.read_text(os.path.join(user_root, user, "user.xml"))
    if result.status == READ_MISSING:
        return _Vita3kListedUser(user, None, _VITA3K_USER_NO_XML)
    if result.status != READ_OK:
        return _Vita3kListedUser(user, None, _VITA3K_USER_XML_UNREADABLE)
    try:
        root = _ET.fromstring(result.text or "")
    except _ET.ParseError:
        return _Vita3kListedUser(user, None, _VITA3K_USER_XML_INVALID)
    id_attr = root.get("id") if root.tag == "user" else None
    identity = _vita3k_stem(user) if id_attr is None else id_attr
    return _Vita3kListedUser(user, identity, _VITA3K_USER_LISTED)


def _vita3k_listed_users(
    machine: Machine, user_root: str, users: tuple[str, ...]
) -> tuple[_Vita3kListedUser, ...]:
    """The users Vita3K itself would list — get_users_list read the way it runs."""
    return tuple(_vita3k_listed_user(machine, user_root, user) for user in users)


@dataclass(frozen=True, slots=True)
class _Vita3kUser:
    """What config.yml records about which user a launch would open.

    ``headline`` is the recorded user where the emulator's own listing holds
    it — the one user a frontend launch reopens, and so the tree the answer
    names — and ``None`` everywhere the launch's user is not settled by what
    was read.
    """

    configured: str | None
    headline: str | None
    readings: tuple[OptionReading, ...]
    sentence: str
    reason: str


def _vita3k_recorded_user_state(
    configured: str, listed: tuple[_Vita3kListedUser, ...], user_root: str
) -> tuple[str | None, str, str]:
    """The recorded user held against the emulator's own listing — four states.

    Returns ``(headline, sentence, reason)``. The listing holds the recorded
    id — the headline follows it; some user.xml could not be read — whether
    the emulator would list the recorded user is not established, and nothing
    is decided; the recorded directory exists but nothing lists it as that
    user — not set up; or nothing here answers to the id at all — no tree.
    """
    identities = tuple(u.identity for u in listed if u.identity is not None)
    unreadable = tuple(u.directory for u in listed if u.fate == _VITA3K_USER_XML_UNREADABLE)
    own = next((u for u in listed if u.directory == configured), None)
    if configured in identities:
        sentence = (
            f"config.yml records {_VITA3K_USER_ID_KEY} {configured} and that user is "
            "among the ones Vita3K itself would list — the directories under ux0/user "
            "whose user.xml loads, keyed by the file's id or the directory name's stem "
            "(get_users_list, user_management.cpp:83-97), read here the same way — so "
            "a frontend launch, naming an app on the command line, reopens exactly "
            "that user (init_home, gui.cpp:688-696) and the tree named is its, created "
            "on the first save where no directory of that name exists yet; a plain "
            "launch without user-auto-connect opens the user manager instead — every "
            "user directory found is still stated"
        )
        return configured, sentence, "the configured user's tree is the one named"
    if unreadable:
        sentence = (
            f"config.yml records {_VITA3K_USER_ID_KEY} {configured}, and whether "
            "Vita3K would list that user is not established — the user.xml under "
            f"{', '.join(unreadable)} could not be read, and the listing is keyed by "
            "what those files state (get_users_list, user_management.cpp:83-97) — so "
            "the tree named stays the first found, and every user directory found is "
            "stated"
        )
        return None, sentence, "whether the configured user is set up here was not established"
    if own is not None:
        if own.fate == _VITA3K_USER_NO_XML:
            detail = "the directory has no user.xml"
        elif own.fate == _VITA3K_USER_XML_INVALID:
            detail = "its user.xml does not parse"
        else:
            # "it" is the directory: the id may come from the user.xml's
            # own attribute or from the directory name's stem, and the
            # sentence must not claim the file states what the stem does.
            detail = f'it answers to id "{own.identity}" instead'
        sentence = (
            f"config.yml records {_VITA3K_USER_ID_KEY} {configured} and its directory "
            f"exists, but no user.xml here lists it as that user — {detail} — so "
            "Vita3K would skip it and open the user manager for the player to pick "
            "(get_users_list, user_management.cpp:83-97; init_home, gui.cpp:688-696); "
            "the tree named stays the first found, and every user directory found is "
            "stated"
        )
        return None, sentence, "the configured user is not set up here"
    sentence = (
        f"config.yml records {_VITA3K_USER_ID_KEY} {configured}, no directory of "
        f"that name exists below {user_root}, and no user.xml here names that id — "
        "nothing for a launch to reopen, so the user manager opens and the player "
        "picks (init_home, gui.cpp:688-696) — the tree named is the first found, "
        "and every user directory found is stated"
    )
    return None, sentence, "the configured user has no tree here"


def _vita3k_user(
    read: YamlScalars,
    *,
    machine: Machine,
    listing: GlobResult,
    users: tuple[str, ...],
    user_root: str,
) -> _Vita3kUser:
    """The user preselection config.yml records, resolved against the tree.

    Vita3K writes the opened user's id into ``user-id`` (select_and_open_user,
    user_management.cpp:329-331 at cb1f592c) — so the current user *is*
    recorded, contrary to what this answer used to say. What the record is
    worth is the conditional part: ``init_home`` reopens it only when the id
    is among the users the emulator itself listed **and** either the launch
    names an app on the command line or ``user-auto-connect`` is on; otherwise
    the user manager opens and the player picks (gui.cpp:688-696). That list
    is built before ``init_home`` runs (init, gui.cpp:914 then :922) the way
    :func:`_vita3k_listed_users` reads it here — the same on-disk fact, not a
    proxy for it. Both frontends launch by naming an app on the command line —
    ES-DE's Vita3K command passes ``-r``, which is ``--installed-path``
    (config.cpp:260) — so where the recorded id is among the listed users, a
    frontend launch reopens exactly that user and the headline follows it: the
    tree composes from the identity (io.user_id is the gui.users key —
    init_user, user_management.cpp:227), which the first save creates where no
    directory of that name exists yet. Everywhere else nothing read here
    settles the launch's user, and the sentence says what stands in the way —
    including the one state that is atlas's own, a user.xml it could not read,
    which leaves the emulator's listing unknowable rather than decided.
    """
    unread = _VITA3K_USER_ID_KEY in read.skipped
    configured = None if unread else (read.get(_VITA3K_USER_ID_KEY) or None)
    auto = None if _VITA3K_AUTO_CONNECT_KEY in read.skipped else read.get(_VITA3K_AUTO_CONNECT_KEY)
    headline = None
    if unread:
        sentence = (
            f"config.yml states {_VITA3K_USER_ID_KEY} as a construct atlas does not read, so "
            "which user it preselects is unread here — every user directory found is stated"
        )
        reason = "the configured user id is stated in a construct atlas does not read"
    elif configured is None:
        sentence = (
            f"config.yml records no {_VITA3K_USER_ID_KEY}, so nothing preselects a user and "
            "the user manager opens for the player to pick (init_home, gui.cpp:688-696) — "
            "every user directory found here is stated"
        )
        reason = "the configuration preselects no user"
    elif listing.status == GLOB_COMPLETE and users:
        listed = _vita3k_listed_users(machine, user_root, users)
        headline, sentence, reason = _vita3k_recorded_user_state(configured, listed, user_root)
    else:
        # An empty or unlistable tree: _per_user_state answers those states
        # with its own sentences, so what stands here is the config-side fact
        # alone, never read in either state today.
        sentence = (
            f"config.yml records {_VITA3K_USER_ID_KEY} {configured} — Vita3K opens it when "
            "the launch names an app on the command line, and on a plain launch when "
            "user-auto-connect is on (init_home, gui.cpp:688-696); otherwise the user "
            "manager opens and the player picks — so every user directory found here is "
            "stated"
        )
        reason = "the configuration preselects a user, and whether a launch opens it depends "
        reason += "on how the launch was made"
    readings = (
        OptionReading(
            _VITA3K_USER_ID_KEY,
            None if unread else read.get(_VITA3K_USER_ID_KEY),
            _vita3k_key_provenance(
                _VITA3K_USER_ID_KEY,
                configured,
                unread=unread,
                unset="no user is preselected (config.h:189)",
            ),
            None,
        ),
        OptionReading(
            _VITA3K_AUTO_CONNECT_KEY,
            auto,
            _vita3k_key_provenance(
                _VITA3K_AUTO_CONNECT_KEY,
                auto,
                unread=_VITA3K_AUTO_CONNECT_KEY in read.skipped,
                unset="the default false governs (config.h:190)",
            ),
            None,
        ),
    )
    return _Vita3kUser(configured, headline, readings, sentence, reason)


def _vita3k_key_provenance(key: str, value: str | None, *, unread: bool, unset: str) -> str:
    """Where one config.yml key's value came from — the same three states twice.

    A key is stated as a construct the scalar reader passed over, stated as a
    value, or not stated at all, and the two keys this answer reads differ only
    in what governs when nothing is stated. Saying that once keeps the two
    readings from drifting into two accounts of one grammar.
    """
    if unread:
        return f"{key} is stated as a construct atlas does not read — its value is unread, not absent"
    if value:
        return f'config.yml: {key}: "{value}"'
    return f"{key} is unset — {unset}"


def _vita3k_savefile_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavefilePlacement | Unresolved:
    """Vita3K's save answer: the ux0 tree below the preference path.

    ``pref-path`` is the one key that matters, and an empty one means the
    emulator's own default preference path (config.cpp:189-190) — a location
    this build derives at run time rather than writing down, so an unset key
    is a refusal here rather than an invented directory.

    Below it the unit is ``ux0/user/<user>/savedata``, one directory per title
    id (io.cpp:136-143). Which user that is at run time is decided by
    ``init_home`` from the id config.yml records — see :func:`_vita3k_user`.
    Every user directory found becomes a group of its own with the recorded id
    stated beside them, and where the recorded user's directory is among them
    the headline names its tree, because a frontend launch reopens exactly
    that user; everywhere else the headline stays the first tree found and
    the caveat says what is not settled.
    """
    config_path = _standalone_settings_path(card, homes)
    result = machine.read_text(config_path)
    if result.status not in (READ_OK, READ_MISSING):
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"Vita3K's configuration ({config_path}) exists and could not be read — where "
            "its ux0 tree lives is unknowable here",
            {"emulator": card.token, "config": config_path},
        )
    read = read_scalars(result.text or "" if result.status == READ_OK else "")
    if read.refusal is not None:
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"Vita3K's configuration ({config_path}) states a construct atlas does not read "
            f"({read.refusal}) — where its ux0 tree lives is unknowable here",
            {"emulator": card.token, "config": config_path, "reason": read.refusal},
        )
    if _VITA3K_PREF_PATH_KEY in read.skipped:
        # Stated as a nested block, a list or a multi-line scalar: the emulator
        # reads a value here and atlas did not. That is not an unset key, and
        # answering the unset key's refusal would name the wrong reason.
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"Vita3K's configuration ({config_path}) states {_VITA3K_PREF_PATH_KEY} as a "
            "construct atlas does not read — its value is unread, not absent, so where its "
            "ux0 tree lives is unknowable here",
            {
                "emulator": card.token,
                "config": config_path,
                "reason": f"{_VITA3K_PREF_PATH_KEY} is unread",
            },
        )
    stated = read.get(_VITA3K_PREF_PATH_KEY)
    if not stated:
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"Vita3K's configuration ({config_path}) names no pref-path, and an empty one "
            "means a default this build derives at run time rather than writing down "
            "(config.cpp:189-190) — where its ux0 tree lives is not established here",
            {"emulator": card.token, "config": config_path},
        )
    host = sandbox.host(_VITA3K_PREF_PATH_KEY, stated)
    if host.path is None:
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
            f"the preference path Vita3K's configuration names ({stated!r}) has no "
            f"spelling on this host — {config_path} read fine, and nothing this answer "
            "could anchor at",
            {"emulator": card.token, "config": config_path, "path": stated},
        )
    user_root = os.path.join(host.path, _VITA3K_USER_TREE)
    listing, users = _per_user_listing(machine, user_root)
    user = _vita3k_user(read, machine=machine, listing=listing, users=users, user_root=user_root)
    # An empty tree with a recorded user is still an empty tree — the headline
    # stays the compiled default — but the emptiness now says one thing more:
    # the recorded user's tree is among the ones missing.
    recorded_aside = "" if user.configured is None else f", the recorded user {user.configured} included"
    return _per_user_savedata_placement(
        machine,
        card=card,
        listing=listing,
        users=users,
        shape=_PerUserSaves(
            user_root=user_root,
            first_user=_VITA3K_FIRST_USER,
            names_citation="init_savedata_app_path, io.cpp:136-143 at commit cb1f592c",
            user_sentence=user.sentence,
            no_user_sentence=(
                f"no user directory exists below {user_root} "
                f"— nothing has saved here yet{recorded_aside}. The tree named is the one "
                f"the emulator's own redirect spells, user {_VITA3K_FIRST_USER} "
                "(io.cpp:203), which it would create; it is not a directory found on this "
                "machine"
            ),
            user_reason=user.reason,
            mode="pref-path",
            readings=(
                OptionReading(
                    _VITA3K_PREF_PATH_KEY,
                    stated,
                    f'config.yml: {_VITA3K_PREF_PATH_KEY}: "{stated}"',
                    None,
                ),
                *user.readings,
            ),
            reading_file=config_path if result.status == READ_OK else None,
            provenance=(
                f"standalone save card '{card.token}': the preference path from config.yml "
                "(config.cpp:189-190 at commit cb1f592c)"
            ),
            configured_user=user.configured,
            headline_user=user.headline,
        ),
        extra_caveats=extra_caveats,
    )


_STANDALONE_SAVE_RESOLVERS = {
    "DOLPHIN": _dolphin_savefile_placement,
    "PRIMEHACK": _dolphin_savefile_placement,
    "PPSSPP": _ppsspp_savefile_placement,
    "XEMU": _xemu_savefile_placement,
    "CEMU": _cemu_savefile_placement,
    "AZAHAR": _azahar_savefile_placement,
    "DUCKSTATION": _duckstation_savefile_placement,
    "PCSX2": _pcsx2_savefile_placement,
    "MELONDS": _melonds_savefile_placement,
    "RPCS3": _rpcs3_savefile_placement,
    "VITA3K": _vita3k_savefile_placement,
}

# Which citation slots each reading speaks, so a card can be crossed with the
# code that reads it: a slot the reading names and the card omits fails the
# answer, and a slot the card states and no reading names is evidence written
# for nothing.
STANDALONE_SAVE_CITATION_SLOTS = {
    "DOLPHIN": _DOLPHIN_CITATION_SLOTS,
    "PRIMEHACK": _DOLPHIN_CITATION_SLOTS,
}


def _standalone_savefile_placement(
    machine: Machine,
    *,
    card: StandaloneSaveCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavefilePlacement | Unresolved:
    """Dispatch to the emulator's own resolver — a card without one fails loudly.

    The launch command rides along because an emulator's own flags can outrank
    its configuration (Cemu's ``--mlc``), and the catalogue command is the one
    read that says whether this launch carries any. The content path rides for
    the one hole a resolver can fill itself — DuckStation's file-title mode
    names the card after the content's own stem.
    """
    resolver = _STANDALONE_SAVE_RESOLVERS.get(card.token)
    if resolver is None:
        raise ValueError(
            f"standalone save card {card.token!r} has no resolver registered — the card and "
            "the code shipped out of step"
        )
    return resolver(
        machine,
        card=card,
        homes=homes,
        sandbox=sandbox,
        system=system,
        command=command,
        extra_caveats=extra_caveats,
        content_path=content_path,
    )


def _standalone_savefile_unresolved(spec: EmulatorSpec) -> Unresolved:
    """The refusal for a standalone entry no packaged save card covers."""
    return Unresolved(
        UNRESOLVED_STANDALONE,
        f"standalone emulator {spec.label!r} ({spec.system}) is not resolvable yet — its save "
        "tree is shaped by a configuration of its own, and only emulators with a packaged "
        "standalone save card are read (issue #3 tracks the rest)",
        {"label": spec.label, "system": spec.system},
    )


# ---------------------------------------------------------------------------
# Standalone savestates (#225) — the savefile dispatch's twin, one question
# over. A standalone emulator's states are its own the way its saves are: a
# compiled join below one of its XDG trees, or a directory its configuration
# names. What every card shares is the naming problem the save side never had:
# the emulator names its states from the running game's own identity (a disc
# serial, a game id, a title id), which no content path derives — melonDS
# alone derives its from the loaded file's name — so the card states the
# cited pattern and the answer hands it over in a caveat instead of listing
# files nobody can name.
# ---------------------------------------------------------------------------


def _standalone_savestate_settings(card: StandaloneSavestateCard) -> emulator_settings.SettingsFile:
    """The settings file this savestate card names, from the one table that addresses it."""
    if card.settings is None:
        raise ValueError(
            f"savestate card {card.token!r} names no settings file and this resolver reads "
            "one — the card and the code shipped out of step"
        )
    return emulator_settings.settings_file(card.token, card.settings)


# The case-insensitive (section, key) match moved to atlas.qt_ini (#295): the
# firmware and DuckStation modules read the same ini files and needed the same
# mirror, and qt_ini is the module all three already import. The private name
# stays bound here because this module is where every route that matches an
# ini key addresses it.
_simpleini_value = qt_ini.simpleini_value


def _savestate_names_caveat(
    card: StandaloneSavestateCard,
    directory: str,
    citation: str | None,
    *,
    reason: str | None = None,
) -> Caveat:
    """The statement every non-derivable naming shares: pattern stated, names refused.

    The pattern is the card's word, cited to the build that composes it, and
    it rides in ``data`` because it is what a client acts on — the shape of
    the files a backup of this tree will contain. *reason* replaces the
    default why-sentence where the refusal is not the usual one — MAME's
    answer refuses names when the launch names no system, or when a
    statename template this reading does not model shapes the subdirectory.
    """
    # An absence card never reaches a names caveat, so both are stated.
    assert card.names is not None and citation is not None
    why = reason or (
        "from the running game's own identity, which follows from nothing atlas reads"
    )
    return Caveat(
        CAVEAT_FILE_NAMES_UNESTABLISHED,
        f"a state below {directory} is named {card.names} — {why} ({citation}) — so the "
        "tree is stated and its entries refused; back it up whole",
        {"core": card.token, "dir": directory, "pattern": card.names, "citation": citation},
    )


def _fixed_savestate_tree_placement(
    machine: Machine,
    *,
    card: StandaloneSavestateCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavestatePlacement | Unresolved:
    """A compiled states tree below the emulator's own directory — five cards' shape.

    Dolphin's ``StateSaves``, PrimeHack's inherited copy of it, PPSSPP's
    ``PSP/PPSSPP_STATE``, RPCS3's ``savestates`` and Azahar's ``states`` are
    the same claim: the build joins the tree itself and no configuration
    moves it, so no file is read — the answer is a path join below the XDG
    base this launch pins, the symlink walk an arrangement reroutes it with,
    and the cited naming pattern in the caveat that says why the files below
    cannot be listed. One resolver serves five cards, so every line number it
    speaks is the card's (:meth:`StandaloneSavestateCard.cite`), per build
    where the builds differ (PrimeHack, #246).
    """
    assert card.base is not None and card.subdir is not None  # the loader pairs them
    directory = os.path.join(homes.emulator_root(card.base, card.token), card.subdir)
    physical, link_caveats = _link_view(machine, directory)
    return SavestatePlacement(
        dir=directory,
        root_kind=STATE_ROOT_EMULATOR_DIRECTORY,
        needs=(),
        file_set=UNKNOWN_FILE_SET,
        sources=(
            f"standalone savestate card '{card.token}': {card.provenance}",
            f"the tree is the build's own join — {card.cite('build', flatpak=homes.flatpak)}, "
            f"{card.cite('tree', flatpak=homes.flatpak)}",
        ),
        caveats=(
            *extra_caveats,
            *link_caveats,
            _savestate_names_caveat(card, directory, card.cite("names", flatpak=homes.flatpak)),
        ),
        physical_dir=physical,
    )


def _pcsx2_savestate_placement(
    machine: Machine,
    *,
    card: StandaloneSavestateCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavestatePlacement | Unresolved:
    """PCSX2's states answer: the directory ``[Folders] Savestates`` names.

    The issue's own trap, settled: RetroDECK writes the key spelled
    ``SaveStates`` (component_prepare.sh:25) while the source reads
    ``"Savestates"`` (Pcsx2Config.cpp:2284 at v2.6.3), and the written line
    governs because SimpleIni matches keys ASCII case-insensitively
    (:func:`_simpleini_value` carries the chain) — so this reading matches
    the key the way the emulator does instead of quoting the compiled
    ``sstates`` over it. The value resolves below the DataRoot the way
    ``LoadPathFromSettings`` resolves it (:func:`_pcsx2_folder_below_dataroot`
    — an empty line moves the directory to the DataRoot itself, it does not
    restore the default), and an absolute value is translated through the
    launch's sandbox rather than trusted as a host path.
    """
    setting = card.directory
    assert setting is not None  # the loader pairs the key with the settings file
    settings = _standalone_savestate_settings(card)
    data_root = homes.emulator_root(settings.bases[0], card.token)
    ini_path = settings.only(
        config_home=homes.base("config"), data_home=homes.base("data"), flatpak=homes.flatpak
    )
    result = machine.read_text(ini_path)
    if result.status not in (READ_OK, READ_MISSING):
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"PCSX2's configuration ({ini_path}) exists and could not be read — where a "
            "state lands is unknowable here",
            {"emulator": card.token, "config": ini_path},
        )
    values = qt_ini.values(result.text) if result.status == READ_OK and result.text else {}
    raw, spelled = _simpleini_value(values, setting.section, setting.key)
    resolved, reading = _pcsx2_folder_below_dataroot(
        raw,
        key=setting.key,
        default=setting.default,
        default_citation=setting.citation,
        data_root=data_root,
        spelled=spelled,
    )
    if resolved is None:
        assert raw is not None  # only an absolute value leaves the helper unresolved
        host = sandbox.host(setting.key, raw)
        if host.path is None:
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
                f"the states directory PCSX2's configuration names ({raw!r}) has no "
                f"spelling on this host — {ini_path} read fine, and nothing this answer "
                "could anchor at",
                {"emulator": card.token, "config": ini_path, "path": raw},
            )
        directory = host.path
    else:
        directory = resolved
    if raw and spelled != setting.key:
        reading += (
            f" — spelled {spelled!r} and read as the {setting.key!r} key, because SimpleIni "
            "matches ASCII case-insensitively (SimpleIni.h:3642-3643, :2916-2931 at v2.6.3)"
        )
    physical, link_caveats = _link_view(machine, directory)
    return SavestatePlacement(
        dir=directory,
        root_kind=STATE_ROOT_EMULATOR_DIRECTORY,
        needs=(),
        file_set=UNKNOWN_FILE_SET,
        sources=(f"standalone savestate card '{card.token}': {card.provenance}", reading),
        caveats=(
            *extra_caveats,
            *link_caveats,
            _savestate_names_caveat(card, directory, card.names_citation),
        ),
        physical_dir=physical,
    )


def _duckstation_savestate_placement(
    machine: Machine,
    *,
    card: StandaloneSavestateCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavestatePlacement | Unresolved:
    """DuckStation's states answer: ``[Folders] SaveStates`` below the probed DataRoot.

    The same two-base DataRoot probe the save answer performs, then the one
    key: default ``savestates`` below the DataRoot, an empty value falling to
    the default and a relative one joining the root (settings.cpp:1944,
    :1955-1962, :1975 at 64655818e — upstream then resolves the path with
    ``Path::RealPath``, which is what makes RetroDECK's symlink at the
    default location work; this answer states the link as ``physical_dir``
    instead of silently resolving it). The ini goes through the same
    ``CSimpleIniA`` as PCSX2's, so the key is matched case-insensitively
    here too.
    """
    setting = card.directory
    assert setting is not None  # the loader pairs the key with the settings file
    data_root, values, stated_ini, root_caveats, refusal = _duckstation_settings(
        machine, homes, card, lost="where a state lands is unknowable here"
    )
    if refusal is not None:
        return refusal
    raw, spelled = _simpleini_value(values, setting.section, setting.key)
    if not raw:
        # Unset and empty answer alike HERE and only here: DuckStation's own
        # LoadPathFromSettings folds an empty value back to the compiled
        # default (`if (value.empty()) value = def;`, settings.cpp:1955-1957
        # at 64655818e). PCSX2's does not — its empty value survives and
        # Path::Combine lands the directory on the DataRoot itself
        # (:func:`_pcsx2_folder_below_dataroot`) — so this branch must not be
        # "fixed" to match the sibling resolver.
        directory = os.path.join(data_root, setting.default)
        reading = (
            f"{setting.key} is unset or empty — the default {setting.default} below the "
            f"DataRoot governs ({setting.citation})"
        )
    elif not os.path.isabs(raw):
        # The emulator's own combine (settings.cpp:1958-1959), not
        # os.path.join: what that changes for a degenerate spelling, and what
        # of the RealPath tail an answer mirrors, is stated once at
        # :func:`atlas.duckstation.load_path`.
        directory = qt_ini.path_combine(data_root, raw)
        reading = (
            f'settings.ini: [{setting.section}] {spelled} = "{raw}" — a relative value '
            "joins the DataRoot (LoadPathFromSettings, settings.cpp:1955-1962)"
        )
    else:
        host = sandbox.host(setting.key, raw)
        if host.path is None:
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
                f"the states directory DuckStation's configuration names ({raw!r}) has "
                f"no spelling on this host — {stated_ini} read fine, and nothing this "
                "answer could anchor at",
                {"emulator": card.token, "config": stated_ini or "", "path": raw},
            )
        directory = host.path
        reading = f'settings.ini: [{setting.section}] {spelled} = "{raw}"'
    physical, link_caveats = _link_view(machine, directory)
    return SavestatePlacement(
        dir=directory,
        root_kind=STATE_ROOT_EMULATOR_DIRECTORY,
        needs=(),
        file_set=UNKNOWN_FILE_SET,
        sources=(f"standalone savestate card '{card.token}': {card.provenance}", reading),
        caveats=(
            *extra_caveats,
            *root_caveats,
            *link_caveats,
            _savestate_names_caveat(card, directory, card.names_citation),
        ),
        physical_dir=physical,
    )


_MELONDS_STATE_SLOTS = tuple(range(1, 9))


def _melonds_state_files(
    card: StandaloneSavestateCard, content_path: str | None
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[Caveat, ...]]:
    """The eight slot names: the content's stem where it derives, the open hole where not.

    melonDS is the one standalone emulator whose state names derive from the
    content path — ``<rom stem>.ml1`` through ``.ml8`` (getSavestateName,
    EmuInstance.cpp:696-701; slots 1-8, Window.cpp:356-361 at 1.1) — so the
    declared set is concrete exactly where the save answer's is, and holds
    the ``<rom_stem>`` hole open for an archive or an unnamed content the
    same way (slot 0 is the free-file picker and names nothing).
    """
    # melonDS's card states its tree, so its names are stated too — only an
    # absence card carries none, and one never reaches a resolver.
    assert card.names is not None and card.names_citation is not None
    if content_path is not None and not _melonds_is_archive(content_path):
        stem = _melonds_stem(os.path.basename(content_path))
        return tuple(f"{stem}.ml{slot}" for slot in _MELONDS_STATE_SLOTS), (), ()
    if content_path is not None:
        sentence = (
            "the content is an archive — melonDS names the state after the file inside it "
            "(EmuInstance.cpp:1846-1848), which the archive's own path does not derive; "
            "fill <rom_stem> with the archived file's name without its last extension"
        )
    else:
        sentence = (
            "a state is named after the loaded file — its name without the last extension, "
            "and for a ROM inside an archive the name of the file inside it "
            "(EmuInstance.cpp:1884, :1846-1848); fill <rom_stem> with that name"
        )
    caveat = Caveat(
        CAVEAT_FILENAMES_CONTENT_CONDITIONAL,
        sentence,
        {
            "core": card.token,
            "files": card.names,
            "rom_stem": "the loaded file's name without its last extension — for an "
            "archive, the archived file's",
            "citation": card.names_citation,
        },
    )
    return (
        tuple(f"{TEMPLATE_ROM_STEM}.ml{slot}" for slot in _MELONDS_STATE_SLOTS),
        (HOLE_ROM_STEM,),
        (caveat,),
    )


def _melonds_savestate_placement(
    machine: Machine,
    *,
    card: StandaloneSavestateCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavestatePlacement | Unresolved:
    """melonDS's states answer: ``<rom stem>.ml1``–``.ml8`` where SavestatePath points.

    The save answer's twin down to the branch structure, because upstream is
    the same function: getSavestateName hands ``[Instance0] SavestatePath``
    to the getAssetPath the save path goes through (EmuInstance.cpp:696-701
    at 1.1), so the configured directory, the beside-the-ROM default of an
    empty value and the cwd-relative case all fall exactly as they do for
    the ``.sav``.
    """
    config = _melonds_config(
        machine,
        homes,
        card,
        key="SavestatePath",
        what="state",
        lost="where every game's states land is unknowable here",
    )
    if isinstance(config, Unresolved):
        return config
    root = _melonds_root(config, card, sandbox, content_path, key="SavestatePath", what="states")
    if root.refusal is not None:
        return root.refusal
    files, name_needs, name_caveats = _melonds_state_files(card, content_path)
    caveats: list[Caveat] = [*extra_caveats, *root.caveats, *name_caveats]
    if root.directory.startswith("<"):
        physical = None
    else:
        physical, link_caveats = _link_view(machine, root.directory)
        caveats.extend(link_caveats)
    assert root.root_kind in STATE_ROOT_KINDS  # _melonds_root anchors nowhere else
    return SavestatePlacement(
        dir=root.directory,
        root_kind=cast("StateRootKind", root.root_kind),
        needs=(*root.needs, *name_needs),
        file_set=FileSet(
            FILE_SET_DECLARED,
            files,
            f"declared by standalone savestate card '{card.token}'",
            complete=False,
        ),
        sources=(
            f"standalone savestate card '{card.token}': {card.provenance}",
            config.provenance,
        ),
        caveats=tuple(caveats),
        physical_dir=physical,
    )


def _xemu_savestate_placement(
    machine: Machine,
    *,
    card: StandaloneSavestateCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavestatePlacement | Unresolved:
    """xemu's states answer: QEMU internal snapshots inside the qcow2 hard disk.

    The save answer's inside-image statement, one question over (#284). A
    snapshot is not a file: ``save_snapshot`` writes the VM state into the
    vmstate block device and creates the snapshot record across every
    snapshot-capable device (migration/savevm.c:3285-3301 at v0.8.135) — of
    which the qcow2 at ``[sys.files] hdd_path`` is the only one — and xemu's
    own snapshot browser reads them back by opening exactly that file
    (ui/xemu-snapshots.c:156-158, the list via ``bdrv_snapshot_list``
    :193-198). So the answer names the image the way the save answer does,
    and the ``savestate-inside-image`` caveat is what keeps "here is the
    file" from reading as "and states lie beside it". A machine with no disk
    configured cannot snapshot at all ("no block device can store vmstate",
    block/snapshot.c:781-782), which is the refusal branch.
    """
    stated = card.inside_image
    assert stated is not None  # the loader pairs the shape with this resolver
    settings = _standalone_savestate_settings(card)
    toml_path = settings.only(
        config_home=homes.base("config"), data_home=homes.base("data"), flatpak=homes.flatpak
    )
    parsed = _xemu_document(
        machine,
        card,
        toml_path,
        lost="where a snapshot would land is unknowable here",
    )
    if isinstance(parsed, Unresolved):
        return parsed
    doc, _ = parsed
    hdd = xemu_file_value(doc, stated.key)
    if not hdd:
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"xemu.toml names no hard-disk image ([sys.files] {stated.key}) — the machine "
            "has no disk to keep a snapshot in, and where one would be attached is "
            "unknowable here",
            {"emulator": card.token, "config": toml_path},
        )
    if os.path.isabs(hdd):
        host = sandbox.host(stated.key, hdd)
        if host.path is None:
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
                f"the hard-disk image xemu's configuration names ({hdd!r}) has no spelling "
                f"on this host — {toml_path} read fine, and nothing this answer could "
                "anchor at",
                {"emulator": card.token, "config": toml_path, "path": hdd},
            )
        directory, image = os.path.split(host.path)
        image_path = host.path
        root_kind: StateRootKind = STATE_ROOT_EMULATOR_DIRECTORY
        needs: tuple[str, ...] = ()
        physical, anchor_caveats = _link_view(machine, directory)
        reading = f'xemu.toml: [sys.files] {stated.key} = "{hdd}"{host.note}'
    else:
        # A relative value anchors at the launching process's working
        # directory (the save route's fact, one question over), so the image
        # stays a <cwd> template with the hole the caller fills — no read of
        # the machine can walk links on a directory that is not on it.
        head, image = os.path.split(hdd)
        directory = os.path.join(TEMPLATE_CWD, head) if head else TEMPLATE_CWD
        image_path = os.path.join(directory, image)
        root_kind = STATE_ROOT_WORKING_DIRECTORY
        needs = (HOLE_CWD,)
        physical = None
        anchor_caveats = (_xemu_launch_dependent_caveat(card.token, stated.key, hdd),)
        reading = f'xemu.toml: [sys.files] {stated.key} = "{hdd}"'
    return SavestatePlacement(
        dir=directory,
        root_kind=root_kind,
        needs=needs,
        file_set=FileSet(
            FILE_SET_DECLARED,
            (image,),
            f"declared by standalone savestate card '{card.token}'",
            complete=False,
        ),
        sources=(f"standalone savestate card '{card.token}': {card.provenance}", reading),
        caveats=(
            *extra_caveats,
            *anchor_caveats,
            Caveat(
                CAVEAT_SAVESTATE_INSIDE_IMAGE,
                f"every snapshot lives inside {image} — a QEMU internal snapshot written "
                "into the qcow2 itself, with no file per state — so back the image up "
                f"whole; entries inside it are named {card.names} ({stated.citation})",
                {
                    "emulator": card.token,
                    "image": image_path,
                    "names": card.names or "",
                    "citation": stated.citation,
                },
            ),
        ),
        physical_dir=physical,
    )


# MAME's state files: the menu slots are single keys a-z / 0-9
# (keyboard_input_item_name, ui/state.cpp:34-42 at mame0287), the quick save is
# the literal "quick" (ui/ui.cpp:1702-1705) and the autosave the literal "auto"
# (machine.cpp:429-432, written only for machines whose drivers are flagged and
# only under the off-by-default autosave option, emuopts.cpp:71) — each becomes
# <name>.sta below the per-machine subdirectory (compose_saveload_filename,
# machine.cpp:576). The set is declared, never complete: a -state load may name
# anything, and plugins can save under names of their own.
_MAME_SLOT_NAMES = tuple(
    f"{slot}.sta" for slot in ("auto", "quick", *"0123456789", *"abcdefghijklmnopqrstuvwxyz")
)


@dataclass(frozen=True, slots=True)
class _MameLaunch:
    """What a MAME catalogue command states: ini path, working dir, machine.

    ``machine_is_content`` marks the arcade shape, where the positional MAME
    system IS the ROM's basename (``%BASENAME%``) — the subdirectory then
    derives from the content path or stays a ``<rom_stem>`` hole.
    """

    inipath: str | None
    startdir: str | None
    machine_name: str | None
    machine_is_content: bool


def _mame_command_prefix(tokens: list[str]) -> tuple[str | None, int | None]:
    """ES-DE's own prefix: the ``%STARTDIR%`` value and the emulator token's index."""
    startdir = None
    for i, token in enumerate(tokens):
        if token.startswith("%STARTDIR%="):
            startdir = token[len("%STARTDIR%=") :]
        if token.startswith("%EMULATOR_"):
            return startdir, i
    return startdir, None


def _mame_arguments(rest: list[str]) -> tuple[str | None, str | None]:
    """MAME's argument grammar: ``(-inipath value, the positional system word)``.

    Every ``-flag`` consumes the next token as its value (all the catalogue's
    flags do: -inipath, -rompath, the media mounts, the autoboot pair, the
    slot options), and the first token no flag consumed is the positional
    system name.
    """
    inipath = None
    i = 0
    while i < len(rest):
        token = rest[i]
        if not token.startswith("-"):
            return inipath, token
        if token == "-inipath" and i + 1 < len(rest):
            inipath = rest[i + 1]
        i += 2
    return inipath, None


def _mame_launch_reading(command: str) -> _MameLaunch:
    """The command parsed the way ES-DE hands it to MAME.

    Everything before the ``%EMULATOR_…%`` token is ES-DE's own prefix — a
    ``%STARTDIR%=<dir>`` there is the working directory ES-DE changes into
    before launching (:func:`_mame_command_prefix`); after the token MAME's
    own grammar yields the ini path and the positional system word
    (:func:`_mame_arguments`). A command that cannot be tokenized states
    nothing rather than something half-read.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _MameLaunch(None, None, None, False)
    startdir, emulator_at = _mame_command_prefix(tokens)
    if emulator_at is None:
        return _MameLaunch(None, startdir, None, False)
    inipath, machine = _mame_arguments(tokens[emulator_at + 1 :])
    if machine == "%BASENAME%":
        return _MameLaunch(inipath, startdir, None, True)
    if machine is not None and "%" in machine:
        # Some other template in the machine position — nothing this reading
        # can turn into a directory name.
        machine = None
    return _MameLaunch(inipath, startdir, machine, False)


def _mame_ini_data(data: str) -> str:
    """One line's value: comment cut outside quotes, spaces and one quote pair trimmed."""
    kept: list[str] = []
    in_quotes = False
    for ch in data:
        if ch == '"':
            in_quotes = not in_quotes
        if ch == "#" and not in_quotes:
            break
        kept.append(ch)
    value = "".join(kept).strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value


def _mame_ini_line(line: str) -> tuple[str, str] | None:
    """One line as ``(name, value)`` — or ``None`` for comments and invalid lines."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = stripped.split(None, 1)
    if len(parts) != 2:
        return None
    name, data = parts
    return name, _mame_ini_data(data)


def _mame_ini_values(text: str) -> dict[str, str]:
    """A MAME ini read the way core_options reads it (options.cpp:980-1046).

    Leading whitespace skipped, ``#`` opens a comment (outside quotes), the
    name runs to the first whitespace, the value is the rest with spaces and
    one pair of surrounding quotes trimmed (trim_spaces_and_quotes,
    options.cpp:60-73), an invalid line warns and is skipped, and a duplicate
    key's LAST occurrence wins because an equal-priority set overrides
    (entry::set_value, options.cpp:270) — which the dict assignment mirrors.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        parsed = _mame_ini_line(line)
        if parsed is not None:
            values[parsed[0]] = parsed[1]
    return values


def _mame_subst_env(value: str, env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """``osd_subst_env`` over the variables this launch pins — or the one it cannot.

    MAME expands a leading ``~`` as $HOME and ``$VAR``/``${VAR}`` from the
    process environment, dropping a missing variable to nothing with a
    warning (posixdir.cpp:236-300 at mame0287). Atlas cannot enumerate the
    launch's environment, only the variables the sandbox pins (HOME, and the
    XDG homes flatpak force-sets, flatpak-context.c:3174-3177 at 1.16.6) —
    so a variable outside that set answers ``(None, its name)`` and the
    caller refuses instead of guessing which way the process would expand it.
    """
    result: list[str] = []
    i = _mame_tilde_head(value, env, result)
    if i is None:
        return None, "HOME"
    while i < len(value):
        ch = value[i]
        if ch != "$":
            result.append(ch)
            i += 1
            continue
        name, literal, i = _mame_env_reference(value, i + 1)
        if name is None:
            result.append(literal)
            break
        if name not in env:
            return None, name or "$"
        result.append(env[name])
    return "".join(result), None


def _mame_tilde_head(value: str, env: Mapping[str, str], result: list[str]) -> int | None:
    """The leading ``~``: $HOME where it expands, ``None`` where none is pinned.

    A ``~`` followed by anything but a separator stays literal
    (posixdir.cpp:248-252); the scan resumes at the returned index.
    """
    if not value.startswith("~"):
        return 0
    home = env.get("HOME")
    if home is None:
        return None
    if len(value) == 1 or value[1] == "/":
        result.append(home)
        return 1
    return 0


def _mame_env_reference(value: str, i: int) -> tuple[str | None, str, int]:
    """One ``$``-reference at *i*: ``(name, literal-tail, index after)``.

    ``${VAR}`` and bare ``$VAR`` yield the name; an unclosed ``${`` is no
    reference at all and comes back as the literal tail the caller appends.
    """
    if i < len(value) and value[i] == "{":
        end = value.find("}", i)
        if end == -1:
            return None, "$" + value[i:], len(value)
        return value[i + 1 : end], "", end + 1
    start = i
    while i < len(value) and (value[i] == "_" or value[i].isalnum()):
        i += 1
    return value[start:i], "", i


def _mame_env(homes: _XdgHomes, sandbox: _Sandbox) -> dict[str, str]:
    """The environment variables this launch pins — never the whole environment."""
    env: dict[str, str] = {}
    if sandbox.expansion_home:
        env["HOME"] = sandbox.expansion_home
    if homes.xdg_pinned:
        # flatpak force-sets the XDG homes to the app's own .var trees
        # (flatpak-context.c:3174-3177 at 1.16.6), host-spelled.
        env["XDG_CONFIG_HOME"] = homes.config
        env["XDG_DATA_HOME"] = homes.data
    return env


# What the SHIPPED builds compile as the ini search path when no -inipath is
# given. Upstream's #ifndef default is "$HOME/.APP_NAME;.;ini"
# (sdl3/sdlopts.cpp:27-35 at mame0287), but a build define replaces it, and
# both builds atlas describes are Flathub-manifest builds that define exactly
# this: the manifest passes SDL_INI_PATH='$$$$HOME/.APP_NAME;/app/share/
# APP_NAME/ini' (org.mamedev.MAME.yaml:33) and installs a real mame.ini at
# /app/share/mame/ini (:68). The shipped RetroDECK component binary carries
# the literal as bytes — "OME/.APP_NAME;/app/share/APP_NAME/ini" at offset
# 366643824, the "$H" head an instruction immediate the tripwire cannot reach
# — and the APP_NAME halves become "mame" at startup (the ctor strreplace,
# sdl3/sdlopts.cpp:112-115). The upstream ".;ini" tail is UNREACHABLE in
# these builds and must never be stated for them.
_MAME_COMPILED_INI_PATH = ("$HOME/.mame", "/app/share/mame/ini")


@dataclass(frozen=True, slots=True)
class _MameIniElement:
    """One inipath element as this host can probe it — or why it cannot."""

    stated: str
    resolved: str | None


def _mame_ini_elements(
    launch: _MameLaunch,
    env: Mapping[str, str],
    sandbox: _Sandbox,
    cwd: str | None,
) -> tuple[_MameIniElement, ...]:
    """The ini search path, element by element, the way the launch resolves it.

    The command's ``-inipath`` outranks everything (a CLI value cannot be
    overridden from an ini, options.cpp:270); without one the shipped builds'
    compiled default is ``$HOME/.mame;/app/share/mame/ini``
    (:data:`_MAME_COMPILED_INI_PATH` — the Flathub build define, byte-proven
    in the shipped binary, not upstream's ``#ifndef`` fallback). The
    ``/app/...`` element resolves through the sandbox against the running
    deploy — a deploy that does not carry the tree answers no host path,
    which is exactly what the emulator would find inside its sandbox. A
    relative element (possible only in a hand-written ``-inipath``) resolves
    against the working directory only the launch knows.
    """
    stated = launch.inipath.split(";") if launch.inipath else list(_MAME_COMPILED_INI_PATH)
    elements: list[_MameIniElement] = []
    for element in stated:
        substituted, _ = _mame_subst_env(element, env)
        if substituted is None:
            elements.append(_MameIniElement(element, None))
            continue
        if not os.path.isabs(substituted):
            if cwd is None:
                elements.append(_MameIniElement(element, None))
                continue
            substituted = os.path.normpath(os.path.join(cwd, substituted))
            elements.append(_MameIniElement(element, substituted))
            continue
        host = sandbox.host("inipath", substituted)
        elements.append(_MameIniElement(element, host.path))
    return tuple(elements)


def _mame_layer_suspects(
    machine: Machine, directory: str, *, read: tuple[str, ...]
) -> tuple[list[str], bool]:
    """One search-path element's unread ini files, and whether its listing failed.

    *read* is every file name this reading opened. Excluding them by NAME
    across all elements is what the emulator does: ``emu_file`` stops at the
    first element that holds the name (fileio.cpp:374-384), so a second
    ``mame.ini`` — or a second ``<system>.ini`` — further down the path is
    never parsed and is no unread layer.
    """
    suspects: list[str] = []
    incomplete = False
    for pattern in (_ANY_INI_GLOB, os.path.join("source", _ANY_INI_GLOB)):
        listing = machine.glob(os.path.join(directory, pattern))
        if listing.status != GLOB_COMPLETE:
            incomplete = True
        for path in listing.matches:
            name = os.path.relpath(path, directory)
            # ui.ini is the UI manager's own file; parse_standard_inis
            # never opens it, and the files this reading read are no layer.
            if name not in read and name != "ui.ini":
                suspects.append(os.path.join(directory, name))
    return suspects, incomplete


def _mame_driver_ini_clause(*, file: str, driver_ini: str | None, word: str | None) -> str:
    """Why the driver ini was or was not read — the statement's middle clause."""
    if driver_ini is not None:
        return (
            f", and this answer read {driver_ini} — the driver ini, the highest-priority "
            "member of that set (mameopts.h:31-39)"
        )
    if word is not None:
        return (
            f", and no {word}.ini — the driver ini for the system this launch names — was "
            "found along that search path"
        )
    return (
        ", and the launch names no system this reading can turn into a driver ini file "
        f"name, so only {file} was read"
    )


def _mame_standard_ini_layer(
    machine: Machine,
    elements: tuple[_MameIniElement, ...],
    *,
    file: str,
    key: str,
    read: tuple[str, ...],
    driver_ini: str | None,
    word: str | None,
) -> list[Caveat]:
    """The rest of the per-machine ini layer, checked instead of assumed.

    After mame.ini, MAME parses debug/orientation/screen/source/parent/driver
    inis out of the same search path (parse_standard_inis,
    mameopts.cpp:37-96) — including ``source/<sourcefile>.ini`` one directory
    DOWN (the basename is composed with the ``source/`` prefix, :85-87). The
    driver ini is the one member this reading can name, because MAME selects
    it by ``cursystem->name`` (:96) and the launch states that word; the
    others are selected by the driver's orientation flag (:56-59), its screen
    devices (:62-82), its source file (:86) and its clone chain (:90-95) —
    all compiled into the binary and unreadable here.

    So this says what is left over: only where ini files this reading did not
    open actually sit (or where a directory cannot be listed) does the answer
    say a layer went unread, and it says which member it DID read.
    """
    suspects: list[str] = []
    incomplete = False
    for element in elements:
        if element.resolved is None:
            continue
        found, failed = _mame_layer_suspects(machine, element.resolved, read=read)
        suspects.extend(found)
        incomplete = incomplete or failed
    if not suspects and not incomplete:
        return []
    named = ", ".join(sorted(suspects)) if suspects else "the directory could not be listed"
    data = {"key": key, "files": named}
    if driver_ini is not None:
        data["read"] = driver_ini
    return [
        Caveat(
            CAVEAT_PER_GAME_LAYER_UNREAD,
            f"MAME layers per-machine and per-orientation ini files over {file} "
            f"(parse_standard_inis, mameopts.cpp:37-96)"
            + _mame_driver_ini_clause(file=file, driver_ini=driver_ini, word=word)
            + f", and the path holds more than the files this answer read ({named}) — which "
            "of those MAME opens follows from the driver's orientation flag, screen type, "
            "source file and clone chain, all compiled into the binary. Each parses BELOW "
            f"the driver ini (mameopts.h:31-39), so a {key} line in one of them governs only "
            "where the driver ini states none",
            data,
        ),
    ]


def _mame_savestate_placement(
    machine: Machine,
    *,
    card: StandaloneSavestateCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavestatePlacement | Unresolved:
    """MAME's states answer: ``state_directory`` from the ini the launch names.

    The launch_ini shape's one resolver (#284). The governing mame.ini is
    wherever the command's ``-inipath`` points — RetroDECK passes
    ``/var/config/mame/ini``, a sandbox spelling this reading translates —
    falling back to the shipped builds' compiled
    ``$HOME/.mame;/app/share/mame/ini`` search path
    (:data:`_MAME_COMPILED_INI_PATH` — the Flathub build define, byte-proven;
    NOT upstream's ``#ifndef`` fallback), and the file is read with MAME's
    own grammar and priority rules
    (:func:`_mame_ini_values`). A relative ``state_directory`` — the compiled
    ``sta`` default when no ini exists — resolves against the working
    directory, which is the command's ``%STARTDIR%`` where it states one and
    an open hole where it does not. Below the root sits one subdirectory per
    machine (``statename``, default ``%g`` = the running system's short
    name, machine.cpp:474-547, :576), which the command's positional system
    word fills — or the content's own stem where the machine IS the ROM
    (``%BASENAME%``).

    That same word names the ``<system>.ini`` MAME layers over mame.ini
    (``cursystem->name``, mameopts.cpp:96), so this reading OPENS it
    (:func:`_mame_driver_layer`) rather than announcing it: it is the highest
    priority ini of the standard set (mameopts.h:38), so a ``state_directory``
    or ``statename`` line there governs and nothing else in the set can
    overturn it. The members that remain unreadable from outside — the
    orientation, screen, source, parent and grandparent inis — are selected by
    driver metadata compiled into the binary, and are stated where files that
    could be them actually sit
    (:func:`_mame_standard_ini_layer`).

    Two facts ride every answer as caveats: whether the launched system's
    driver is flagged MACHINE_SUPPORTS_SAVE is compiled into the binary and
    unreadable here (states are written and warned about either way,
    machine.cpp:927-928), and any per-machine ini layer that was seen but not
    read.

    Every OTHER MAME answer atlas emits stays silent about this layer, and
    each for a reason that was checked rather than assumed:

    * ``savefile``, ``texture`` and ``mod`` refuse with
      ``standalone-unsupported`` before any directory is computed — MAME has
      no save, texture or mod card (``atlas/data/standalone_saves.json``,
      ``texture_packs.json``, ``mods.json`` name no MAME token), so
      ``nvram_directory``, ``diff_directory`` and ``share_directory`` are
      keys atlas states nothing about at all. There is no claim for the layer
      to qualify.
    * ``screenshot`` is the installation's own route answering the frontend's
      screenshot directory (it carries ``no-core`` for every standalone
      entry), not MAME's ``snapshot_directory``.
    * ``rom_location`` answers the arrangement's ROM tree, and every MAME
      command in both catalogues passes ``-rompath`` on the command line
      (OPTION_PRIORITY_CMDLINE = 151, mameopts.h:27-28), which no ini at any
      priority can override (options.cpp:270).
    * MAME has no firmware card, so no firmware answer names it.
    """
    shape = card.launch_ini
    assert shape is not None  # the loader pairs the shape with this resolver
    launch = _mame_launch_reading(command)
    env = _mame_env(homes, sandbox)
    cwd = _mame_launch_cwd(launch, env)
    ini = _mame_governing_ini(
        machine, token=card.token, file=shape.file, launch=launch, env=env,
        sandbox=sandbox, cwd=cwd,
    )
    if isinstance(ini, Unresolved):
        return ini
    key = "state_directory"
    stated_key = shape.keys.get(key)
    if stated_key is None:
        raise ValueError(
            f"savestate card {card.token!r} states no {key!r} ini key and this resolver "
            "reads it — the card and the code shipped out of step"
        )
    layered = _mame_driver_layer(
        machine, ini, token=card.token, launch=launch, content_path=content_path
    )
    if isinstance(layered, Unresolved):
        return layered
    value = _mame_root_value(card, shape, ini, layered, env, key=key, stated_key=stated_key)
    if isinstance(value, Unresolved):
        return value
    anchored = _mame_root_anchor(
        card, shape, value[0], value[1], key=key, sandbox=sandbox, cwd=cwd,
        launch=launch, stated_ini=layered.stated_in(key, ini),
    )
    if isinstance(anchored, Unresolved):
        return anchored
    machine_dir = _mame_state_subdir(layered.values, launch, content_path)
    directory = (
        os.path.join(anchored.root, machine_dir.subdir)
        if machine_dir.subdir is not None
        else anchored.root
    )
    reading = anchored.reading + machine_dir.reading_suffix
    caveats: list[Caveat] = [
        *extra_caveats,
        *anchored.caveats,
        *machine_dir.caveats,
        *_mame_standard_ini_layer(
            machine,
            ini.layer_elements,
            file=shape.file,
            key=key,
            read=layered.read_names(shape.file),
            driver_ini=layered.driver_ini,
            word=layered.word,
        ),
        Caveat(
            CAVEAT_SAVESTATE_SUPPORT_MACHINE_DEPENDENT,
            "whether the launched system's driver is flagged MACHINE_SUPPORTS_SAVE is "
            "compiled per machine (gamedrv.h:76) and not readable here — an unflagged "
            "one still writes the file and warns that save states are not officially "
            "supported for it (machine.cpp:927-928), so reliability is the driver's own",
            {
                "emulator": card.token,
                "citation": "gamedrv.h:76, machine.cpp:927-928 at mame0287",
            },
        ),
    ]
    # A templated directory is a shape, not a path: walking <cwd>/... or
    # .../mame-sa/<rom_stem> through the filesystem would read the template
    # text as real components and report their absence as a dead link (the
    # family guard at the Dolphin card's region template, same shape).
    physical, link_caveats = (
        _link_view(machine, directory)
        if not directory.startswith("<") and TEMPLATE_ROM_STEM not in directory
        else (None, ())
    )
    caveats.extend(link_caveats)
    if machine_dir.subdir is None:
        file_set = UNKNOWN_FILE_SET
        caveats.append(
            _savestate_names_caveat(
                card, directory, card.names_citation, reason=machine_dir.open_reason
            )
        )
    else:
        file_set = FileSet(
            FILE_SET_DECLARED,
            _MAME_SLOT_NAMES,
            f"declared by standalone savestate card '{card.token}'",
            complete=False,
        )
    return SavestatePlacement(
        dir=directory,
        root_kind=anchored.root_kind,
        needs=tuple(dict.fromkeys((*anchored.needs, *machine_dir.needs))),
        file_set=file_set,
        sources=(
            f"standalone savestate card '{card.token}': {card.provenance}",
            f"{reading} — {_mame_ini_provenance(ini, layered, shape.file)}",
        ),
        caveats=tuple(caveats),
        physical_dir=physical,
    )


def _mame_launch_cwd(launch: _MameLaunch, env: Mapping[str, str]) -> str | None:
    """The working directory the launch pins — ``%STARTDIR%``, resolved, or nothing."""
    if launch.startdir is None:
        return None
    cwd, _ = _mame_subst_env(launch.startdir, env)
    if cwd is not None and not os.path.isabs(cwd):
        return None
    return cwd


@dataclass(frozen=True, slots=True)
class _MameIniReading:
    """One probe pass's outcome: the parsed values and the file that held them."""

    values: Mapping[str, str]
    stated_ini: str | None


@dataclass(frozen=True, slots=True)
class _MameGoverningIni:
    """The governing configuration as the launch resolves it, double parse done.

    ``elements`` is where mame.ini itself was searched; ``layer_elements`` is
    where every LATER ini of the standard set is searched, which is not
    always the same path: ``parse_one_ini`` re-reads ``options.ini_path()``
    on each call (mameopts.cpp:123), so an ``inipath`` line in mame.ini moves
    the layer — that is exactly why mame.ini is parsed twice (:40-42).
    """

    values: Mapping[str, str]
    stated_ini: str | None
    elements: tuple[_MameIniElement, ...]
    layer_elements: tuple[_MameIniElement, ...]


def _mame_ini_probe(
    machine: Machine,
    token: str,
    file: str,
    elements: tuple[_MameIniElement, ...],
    *,
    stop_at: str | None = None,
) -> _MameIniReading | Unresolved:
    """The first *file* on *elements*, read whole — nothing found is an empty reading.

    *stop_at* is the re-probe's early exit: reaching the file the first pass
    already read means nothing moved, and re-reading it would change nothing.
    """
    for element in elements:
        if element.resolved is None:
            continue
        candidate = os.path.join(element.resolved, file)
        if candidate == stop_at:
            break
        result = machine.read_text(candidate)
        if result.status == READ_MISSING:
            continue
        if result.status != READ_OK:
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
                f"MAME's configuration ({candidate}) exists and could not be read — where "
                "a state lands is unknowable here",
                {"emulator": token, "config": candidate},
            )
        return _MameIniReading(_mame_ini_values(result.text or ""), candidate)
    return _MameIniReading({}, None)


def _mame_governing_ini(
    machine: Machine,
    *,
    token: str,
    file: str,
    launch: _MameLaunch,
    env: Mapping[str, str],
    sandbox: _Sandbox,
    cwd: str | None,
) -> _MameGoverningIni | Unresolved:
    """The governing mame.ini: the search-path probe, then the double parse.

    mame.ini is read twice so the first pass can move the ini path itself
    (mameopts.cpp:39-42); a CLI ``-inipath`` cannot be overridden
    (options.cpp:270), so the re-probe happens only without one, and the
    second file's lines override the first's at equal priority.

    Whatever ``inipath`` holds when both passes are done is the path every
    later member of the standard set is searched along, because
    ``parse_one_ini`` constructs its ``emu_file`` from ``options.ini_path()``
    at each call (mameopts.cpp:123).
    """
    elements = _mame_ini_elements(launch, env, sandbox, cwd)
    first = _mame_ini_probe(machine, token, file, elements)
    if isinstance(first, Unresolved):
        return first
    values = dict(first.values)
    stated_ini = first.stated_ini
    if stated_ini is not None and launch.inipath is None and values.get("inipath"):
        moved = _MameLaunch(values["inipath"], launch.startdir, None, False)
        second = _mame_ini_probe(
            machine, token, file, _mame_ini_elements(moved, env, sandbox, cwd),
            stop_at=stated_ini,
        )
        if isinstance(second, Unresolved):
            return second
        if second.stated_ini is not None:
            values = {**values, **second.values}
            stated_ini = second.stated_ini
    layer_elements = elements
    if launch.inipath is None and values.get("inipath"):
        layer_elements = _mame_ini_elements(
            _MameLaunch(values["inipath"], launch.startdir, None, False), env, sandbox, cwd
        )
    return _MameGoverningIni(values, stated_ini, elements, layer_elements)


def _mame_system_word(launch: _MameLaunch, content_path: str | None) -> str | None:
    """The system word this launch hands MAME — the driver ini's own name.

    ``parse_standard_inis`` names the driver ini after ``cursystem->name``
    (mameopts.cpp:96), and ``cursystem`` is the driver ``driver_list::find``
    matches against ``core_filename_extract_base(options.system_name())``
    (:105-109). The launch states that word: the positional token for the
    console and computer rows, and the ROM's own stem where the token is
    ``%BASENAME%``. Where neither is available — a launch that names no
    system, or an arcade row asked without content — there is no file name to
    compose and the answer says so instead of guessing one.
    """
    if launch.machine_is_content:
        if content_path is None:
            return None
        return os.path.splitext(os.path.basename(content_path))[0]
    return launch.machine_name


@dataclass(frozen=True, slots=True)
class _MameLayeredIni:
    """mame.ini with the driver ini layered over it, and which file stated what.

    ``driver_keys`` is the set of option names the driver ini carried, which
    is what lets a reading name the file its value really came from. Every
    one of them outranks mame.ini's: the driver ini parses at
    OPTION_PRIORITY_DRIVER_INI, the highest of the standard set
    (mameopts.h:31-39), and an equal-or-higher priority set overrides
    (options.cpp:270).
    """

    values: Mapping[str, str]
    word: str | None
    driver_ini: str | None
    driver_keys: frozenset[str]

    def stated_in(self, key: str, governing: _MameGoverningIni) -> str | None:
        """The path of the file that stated *key* — ``None`` where nothing did."""
        if key in self.driver_keys:
            return self.driver_ini
        return governing.stated_ini if key in self.values else None

    def read_names(self, file: str) -> tuple[str, ...]:
        """Every ini file name this reading opened, for the leftover-layer check."""
        names = [file]
        if self.driver_ini is not None:
            names.append(os.path.basename(self.driver_ini))
        return tuple(names)


def _mame_driver_layer(
    machine: Machine,
    ini: _MameGoverningIni,
    *,
    token: str,
    launch: _MameLaunch,
    content_path: str | None,
) -> _MameLayeredIni | Unresolved:
    """Read ``<system>.ini`` off the layer's search path and lay it over mame.ini.

    This is the one member of MAME's standard ini set an outside reading can
    name: the emulator picks it by the system's own short name
    (mameopts.cpp:96), and that name follows from the launch — so atlas reads
    it rather than announcing it. Its values are applied over mame.ini's
    because it parses at the higher priority (mameopts.h:38, options.cpp:270),
    and nothing else in the set can overturn them.

    An unreadable file refuses the whole answer, exactly as an unreadable
    mame.ini does: the emulator would have parsed it.
    """
    word = _mame_system_word(launch, content_path)
    if word is None:
        return _MameLayeredIni(ini.values, None, None, frozenset())
    reading = _mame_ini_probe(machine, token, f"{word}.ini", ini.layer_elements)
    if isinstance(reading, Unresolved):
        return reading
    if reading.stated_ini is None:
        return _MameLayeredIni(ini.values, word, None, frozenset())
    return _MameLayeredIni(
        {**ini.values, **reading.values},
        word,
        reading.stated_ini,
        frozenset(reading.values),
    )


def _mame_root_value(
    card: StandaloneSavestateCard,
    shape: SavestateLaunchIni,
    ini: _MameGoverningIni,
    layered: _MameLayeredIni,
    env: Mapping[str, str],
    *,
    key: str,
    stated_key: SavestateIniKey,
) -> tuple[str, str] | Unresolved:
    """The states-root option's value grammar: ``(substituted value, reading)``.

    Set, set-empty and unset are three different claims: a set value gets the
    environment substitution PATH options get, a present-empty one is KEPT —
    the parse hands "" to set_value and the option holds it (options.cpp:1041
    via :262-278), so the compiled default does not come back — and only an
    unset key falls to that default.

    Which FILE stated it is the layered reading's answer, not this one's: the
    driver ini's line outranks mame.ini's, so a value that came from
    ``<system>.ini`` is read back under that name.
    """
    stated_path = layered.stated_in(key, ini)
    stated_file = os.path.basename(stated_path) if stated_path else shape.file
    raw = layered.values.get(key)
    if raw:
        substituted, missing = _mame_subst_env(raw, env)
        if substituted is None:
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
                f"{stated_file} names the states directory through ${missing}, a value of "
                "the launch's own environment this answer cannot establish — where a state "
                "lands is unknowable here",
                {"emulator": card.token, "config": stated_path or shape.file},
            )
        return substituted, f'{stated_file}: {key} {raw}' + (
            "" if substituted == raw else f" — the environment expands it to {substituted}"
        )
    if raw == "":
        return "", (
            f"{stated_file}: {key} is set empty — MAME keeps the empty value "
            "(options.cpp:1041, :262-278), so the states root is the working directory "
            "itself"
        )
    return stated_key.default, (
        f"{key} is unset — the compiled default {stated_key.default!r} governs "
        f"({stated_key.citation})"
    )


@dataclass(frozen=True, slots=True)
class _MameStateRoot:
    """The anchored states root, its kind, and what anchoring it stated."""

    root: str
    root_kind: StateRootKind
    reading: str
    needs: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


def _mame_root_anchor(
    card: StandaloneSavestateCard,
    shape: SavestateLaunchIni,
    substituted: str,
    reading: str,
    *,
    key: str,
    sandbox: _Sandbox,
    cwd: str | None,
    launch: _MameLaunch,
    stated_ini: str | None,
) -> _MameStateRoot | Unresolved:
    """Where the value anchors: a host path, the stated cwd, or the open hole."""
    if os.path.isabs(substituted):
        host = sandbox.host(key, substituted)
        if host.path is None:
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
                f"the states directory MAME's configuration names ({substituted!r}) has "
                f"no spelling on this host — {stated_ini or shape.file} read fine, and "
                "nothing this answer could anchor at",
                {
                    "emulator": card.token,
                    "config": stated_ini or shape.file,
                    "path": substituted,
                },
            )
        return _MameStateRoot(host.path, STATE_ROOT_EMULATOR_DIRECTORY, reading + host.note)
    if cwd is not None:
        return _MameStateRoot(
            os.path.normpath(os.path.join(cwd, substituted)),
            STATE_ROOT_EMULATOR_DIRECTORY,
            reading
            + f" — a relative value resolves against the working directory the launch "
            f"states (%STARTDIR%={launch.startdir})",
        )
    return _MameStateRoot(
        os.path.join(TEMPLATE_CWD, substituted) if substituted else TEMPLATE_CWD,
        STATE_ROOT_WORKING_DIRECTORY,
        reading,
        needs=(HOLE_CWD,),
        caveats=(
            Caveat(
                CAVEAT_SAVE_DIR_LAUNCH_DEPENDENT,
                f"a relative {key} resolves against the launching process's working "
                "directory (emu_file over the searchpath, machine.cpp:899-903), which no "
                "read of this machine can establish",
                {"core": card.token, "path": substituted},
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class _MameSubdir:
    """The per-machine subdirectory — resolved, templated, or honestly open."""

    subdir: str | None
    open_reason: str | None = None
    reading_suffix: str = ""
    needs: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


def _mame_state_subdir(
    values: Mapping[str, str], launch: _MameLaunch, content_path: str | None
) -> _MameSubdir:
    """The subdirectory grammar: statename, default ``%g`` = the machine's own name.

    (get_statename, machine.cpp:474-547, the %g substitution at :544; the
    join at :576.) A present-empty statename is NOT an empty literal:
    get_statename reverts a null or empty option to "%g"
    (machine.cpp:477-478), the one option in this reading whose empty value
    restores the default.
    """
    sname = values.get("statename") or None
    if sname is not None and sname != "%g" and "%" in sname:
        return _MameSubdir(
            None,
            open_reason="the statename template is not modelled",
            caveats=(
                Caveat(
                    CAVEAT_UNKNOWN_OPTION_VALUE,
                    f'statename = "{sname}" templates the subdirectory on a mounted image '
                    "(get_statename, machine.cpp:487-540), which this reading does not "
                    "model — the per-machine subdirectory below the root is not "
                    "established",
                    {"key": "statename", "value": sname},
                ),
            ),
        )
    if sname is not None and sname != "%g":
        literal = sname
        dot = literal.rfind(".")
        if dot != -1:
            literal = literal[:dot]
        return _MameSubdir(
            literal, reading_suffix=f'; statename = "{sname}" names the subdirectory'
        )
    if launch.machine_is_content:
        if content_path is not None:
            return _MameSubdir(os.path.splitext(os.path.basename(content_path))[0])
        return _MameSubdir(TEMPLATE_ROM_STEM, needs=(HOLE_ROM_STEM,))
    if launch.machine_name is not None:
        return _MameSubdir(launch.machine_name)
    return _MameSubdir(
        None,
        open_reason=(
            "the launch names no system, so which machine's subdirectory this run "
            "writes is the user's pick in MAME's own system selection"
        ),
    )


def _mame_ini_provenance(ini: _MameGoverningIni, layered: _MameLayeredIni, file: str) -> str:
    """Which file governed — or that none did, and which elements nobody could probe."""
    unprobed = [e.stated for e in ini.elements if e.resolved is None]
    if ini.stated_ini is not None:
        provenance = f"read from {ini.stated_ini}"
    else:
        provenance = (
            f"no {file} exists on the launch's search path "
            f"({'; '.join(e.stated for e in ini.elements)})"
        )
    if layered.driver_ini is not None:
        provenance += (
            f", with the driver ini {layered.driver_ini} layered over it "
            "(parse_standard_inis, mameopts.cpp:96, at the higher priority of "
            "mameopts.h:38)"
        )
    if unprobed:
        provenance += (
            f" — the element(s) {'; '.join(unprobed)} could not be probed from here "
            "(nothing at that path in the running deploy, or a location only the "
            "launching process resolves)"
        )
    return provenance


_STANDALONE_SAVESTATE_RESOLVERS = {
    "DOLPHIN": _fixed_savestate_tree_placement,
    "PRIMEHACK": _fixed_savestate_tree_placement,
    "PPSSPP": _fixed_savestate_tree_placement,
    "RPCS3": _fixed_savestate_tree_placement,
    "AZAHAR": _fixed_savestate_tree_placement,
    "PCSX2": _pcsx2_savestate_placement,
    "MELONDS": _melonds_savestate_placement,
    "DUCKSTATION": _duckstation_savestate_placement,
    "XEMU": _xemu_savestate_placement,
    "MAME": _mame_savestate_placement,
}

# Which citation slots each savestate reading speaks, so a card can be crossed
# with the code that reads it — the save family's rule, one family over. Only
# the shared fixed-tree resolver speaks card slots: the bespoke readings
# (PCSX2, melonDS, DuckStation) serve one emulator each and carry their lines
# inline, the way their savefile twins do.
_FIXED_SAVESTATE_SLOTS = frozenset({"build", "tree", "names"})
STANDALONE_SAVESTATE_CITATION_SLOTS = {
    "DOLPHIN": _FIXED_SAVESTATE_SLOTS,
    "PRIMEHACK": _FIXED_SAVESTATE_SLOTS,
    "PPSSPP": _FIXED_SAVESTATE_SLOTS,
    "RPCS3": _FIXED_SAVESTATE_SLOTS,
    "AZAHAR": _FIXED_SAVESTATE_SLOTS,
}


def _savestate_absence_answer(
    card: StandaloneSavestateCard,
    *,
    entry: tuple[Caveat, ...] = (),
    arrangement: tuple[Caveat, ...] = (),
) -> SavestateAbsence:
    """The stated no a card's ``absent`` statement becomes — an answer, not a refusal.

    Tree-derived caveats (health findings, link walks) do not ride here: the
    absence names no path, so a statement about this machine's trees
    qualifies nothing it says. What DOES ride is everything that qualifies
    the *claim*: *entry* — the catalogue-status and per-game-override caveats
    the save twin carries, because a gamelist that would launch a DIFFERENT
    emulator for this game is a statement about emulator identity, and "Cemu
    has no savestates" needs the rider that Cemu may not be what runs — the
    card's own ``unverified-version`` where no shipped build pins it (nothing
    ships Ryubing; PICO-8's binary is the user's own copy), and the
    arrangement's evidence caveats — the absence is world knowledge pinned to
    the build an arrangement was verified with, so an arrangement atlas never
    observed, or one observed on another version, says so here exactly as it
    does on every placement (:func:`atlas.evidence.arrangement_caveats`).
    """
    absent = card.absent
    assert absent is not None  # callers branch on the statement first
    caveats: list[Caveat] = [*entry]
    if absent.build_unestablished is not None:
        caveats.append(
            Caveat(
                CAVEAT_UNVERIFIED_VERSION,
                absent.build_unestablished,
                {"emulator": card.token, "verification": "build-unestablished"},
            )
        )
    caveats.extend(arrangement)
    return SavestateAbsence(
        emulator=card.token,
        citation=absent.citation,
        sources=(f"standalone savestate card '{card.token}': {card.provenance}",),
        caveats=tuple(caveats),
    )


def _standalone_savestate_placement(
    machine: Machine,
    *,
    card: StandaloneSavestateCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    system: str,
    command: str,
    extra_caveats: tuple[Caveat, ...],
    content_path: str | None = None,
) -> SavestatePlacement | SavestateAbsence | Unresolved:
    """Dispatch to the emulator's own states resolver — a card without one fails loudly.

    The launch command and content path ride for the same reasons they ride
    the save dispatch: a launch's own flags could outrank configuration, and
    melonDS fills its state names from the content's own stem. An absence
    card never reaches this dispatch: the routes answer it before homes are
    built, because the stated no needs none of what this dispatch carries and
    DOES need the arrangement caveats only a route can supply — reaching here
    with one means a route forgot that branch.
    """
    if card.absent is not None:
        raise ValueError(
            f"standalone savestate card {card.token!r} states the feature does not exist "
            "— the routes answer that before this dispatch, with the arrangement's own "
            "evidence caveats; reaching it here means the route and the dispatch "
            "shipped out of step"
        )
    resolver = _STANDALONE_SAVESTATE_RESOLVERS.get(card.token)
    if resolver is None:
        raise ValueError(
            f"standalone savestate card {card.token!r} has no resolver registered — the card "
            "and the code shipped out of step"
        )
    return resolver(
        machine,
        card=card,
        homes=homes,
        sandbox=sandbox,
        system=system,
        command=command,
        extra_caveats=extra_caveats,
        content_path=content_path,
    )


def _standalone_savestate_unresolved(spec: EmulatorSpec) -> Unresolved:
    """The refusal for a standalone entry no packaged savestate card covers.

    Code and data are the savefile refusal's exactly — and exactly what the
    pre-#225 blanket refusal serialized — so an un-carded emulator's answer
    stays contract-identical to what it always was.
    """
    return Unresolved(
        UNRESOLVED_STANDALONE,
        f"standalone emulator {spec.label!r} ({spec.system}) is not resolvable yet — where "
        "its states land is the emulator's own affair, and only emulators with a packaged "
        "standalone savestate card are read (#225 landed the family)",
        {"label": spec.label, "system": spec.system},
    )


# EmuDeck launches its standalone emulators through per-emulator scripts under
# tools/launchers/, so the catalogue command carries no %EMULATOR_…% token —
# the script's own name is the identity the command states. Only a launcher
# whose wiring has been established maps to a card (issue #3, one emulator per
# sub-issue), never the whole directory by pattern: each entry here says a
# maintainer read that launcher and the config file it leads to. cemu.sh runs
# Cemu against ${HOME}/.config/Cemu/settings.xml (emuDeckCemu.sh:13); azahar.sh
# runs Azahar against ${HOME}/.config/azahar-emu/qt-config.ini
# (emuDeckAzahar.sh:7). The legacy citra.sh stays out: it launches Citra, an
# emulator no card describes. duckstation.sh runs DuckStation against the
# DataRoot the launch environment picks (emuDeckDuckStation.sh:9 assumes the
# environment-unset side, ~/.local/share/duckstation); pcsx2-qt.sh runs PCSX2
# against ${HOME}/.config/PCSX2/inis/PCSX2.ini (emuDeckPCSX2QT.sh:6);
# melonds.sh runs the net.kuribo64.melonDS flatpak against the app's own XDG
# config tree (melonds.sh:4).
_EMUDECK_LAUNCHER_CARDS = {
    "cemu": "CEMU",
    "azahar": "AZAHAR",
    "duckstation": "DUCKSTATION",
    "pcsx2-qt": "PCSX2",
    "melonds": "MELONDS",
    "rpcs3": "RPCS3",
    "vita3k": "VITA3K",
}

# The binary variants an EmuDeck launcher picks between, in its probe order
# (cemu.sh:37-93): an AppImage under ~/Applications (vars.sh:4-5), an
# installed flatpak whose id carries the emulator's name, and otherwise the
# Windows build under Proton. The AppImage variant reads the host's own XDG
# tree; the flatpak variant reads the app's own homes below ~/.var/app, and
# answers only where the settings table names the app id the emulator installs
# as (the rest, and Proton, refuse with the variant named).
_EMUDECK_VARIANT_APPIMAGE = "appimage"
# An emulator EmuDeck extracts out of its AppImage and keeps as a plain
# executable at ``~/Applications/<Name>/<Name>`` (emuDeckVita3K.sh:21-24).
# ES-DE's own find rule lists that path right after the AppImage patterns
# (es_find_rules.xml), so the probe tries it in the same order. Its trees are
# the host's own, because nothing sandboxes it any more than an AppImage.
_EMUDECK_VARIANT_BINARY = "binary"
_EMUDECK_VARIANT_FLATPAK = "flatpak"
_EMUDECK_VARIANT_PROTON = "proton"
_EMUDECK_VARIANT_UNKNOWN = "unestablished"

# The launchers that perform no probe at all: melonds.sh runs the installed
# flatpak unconditionally (melonds.sh:4), so for that script the variant is
# the script's own fact rather than the probe's answer. The ES-DE token route
# keeps the probe — its find rules try AppImage paths before the flatpak
# exports (es_find_rules.xml), the order the probe mirrors.
_EMUDECK_LAUNCHER_PINNED_VARIANTS = {
    "melonds": _EMUDECK_VARIANT_FLATPAK,
}


@dataclass(frozen=True, slots=True)
class _StandaloneLaunch:
    """What a catalogue command identifies: card token, probe name, launcher args.

    ``pinned_variant`` is non-``None`` for a launcher script that picks no
    binary at run time — the variant is then the script's, and the probe
    never runs.
    """

    token: str | None
    probe_name: str | None
    args: tuple[str, ...]
    pinned_variant: str | None = None


@dataclass(frozen=True, slots=True)
class _EmuDeckGate:
    """One launch put through the variant gate: what runs, and what it reads.

    ``homes`` and ``why`` are the two outcomes and never both: bases the
    picked binary reads, or the reason nothing is established for it.
    ``variant`` is ``None`` only where the command identifies no emulator at
    all, which is a different refusal — nothing about a variant is known
    because no launch was recognised.
    """

    launch: _StandaloneLaunch
    variant: str | None
    homes: _XdgHomes | None
    why: str | None


def _emudeck_launcher(command: str) -> tuple[str, tuple[str, ...]] | None:
    """The EmuDeck launcher script a command runs, and the arguments after it.

    An EmuDeck catalogue command is a shell line naming the script by path —
    ``/bin/bash …/tools/launchers/cemu.sh -f -g %ROM%`` — so the script's
    basename identifies the emulator and the trailing arguments carry the
    launcher's own flags (``-w`` forces the Windows build, cemu.sh:79-83).
    """
    tokens = command.split()
    for index, token in enumerate(tokens):
        if token.endswith(".sh") and "/tools/launchers/" in token:
            return os.path.basename(token)[: -len(".sh")], tuple(tokens[index + 1 :])
    return None


def _emudeck_variant_unresolved(spec: EmulatorSpec, token: str, variant: str, why: str) -> Unresolved:
    return Unresolved(
        UNRESOLVED_STANDALONE_VARIANT_UNESTABLISHED,
        f"this entry launches {token} as EmuDeck's {variant!r} variant — {why}",
        {"label": spec.label, "system": spec.system, "variant": variant},
    )


def _mod_enabled(
    machine: Machine,
    *,
    card: ModCard,
    chain: _Chain,
    query: _SaveQuery,
    core_version: str | None,
) -> tuple[bool | None, tuple[str, ...], tuple[Caveat, ...]]:
    """Is mod loading switched on right now — read the way RetroArch reads it?

    The texture route's reading, with one addition this family needed: a card
    may carry the option's **default**. The chain is otherwise identical — the
    options files in RetroArch's own priority order, then the default the
    installed core registers — and the recorded value enters only at the end of
    it, where the machine has said nothing at all.

    That ordering is what keeps the record from overriding a machine: a core
    that registers its options is read, and only a core that registers them too
    late for any probe (FBNeo, LRPS2) reaches the written-down value. Because
    that value is a claim about a build, the card pins the build it was read
    from and a machine running another one gets it with ``unverified-version``
    beside it — the same tripwire the texture family puts on its absent switch,
    and for the same reason.
    """
    if card.option is None:
        return (
            None,
            (f"mod card '{card.key}': no option governs mod loading — whether it is on is not stated",),
            (),
        )
    registered = chain.core.info.options if chain.core.info is not None else None
    live_option = registered.get(card.option.setting) if registered is not None else None
    recorded = card.option.default
    caveats: list[Caveat] = []
    sources: list[str] = []
    effective_default = live_option.default if live_option is not None else None
    if effective_default is None and recorded is not None:
        effective_default = recorded.value
        sources.append(
            f"mod card '{card.key}': {card.option.setting} defaults to \"{recorded.value}\" — "
            f"{recorded.citation}"
        )
        if core_version is not None and recorded.verified_core != core_version:
            caveats.append(
                Caveat(
                    CAVEAT_UNVERIFIED_VERSION,
                    f"that {card.option.setting} defaults to \"{recorded.value}\" was established "
                    f"against core {recorded.verified_core}, and this machine runs {core_version} — "
                    "a build is exactly what could change a default, so the value is not carried "
                    "across the difference unexamined",
                    {
                        "core": card.key,
                        "verification": "drifted",
                        "core_verified": recorded.verified_core,
                        "core_live": core_version,
                    },
                )
            )
    option_gates = _option_gates(
        chain.layers, sandbox=query.sandbox, retroarch_config_dir=chain.retroarch_config_dir
    )
    value, provenance, _, _ = _core_options_value(
        machine,
        override_config_dir=chain.gates.override_config_dir,
        global_file=option_gates.global_file,
        library_name=chain.core.library_name,
        content_dir_name=chain.content.dir_name,
        rom_stem=chain.content.rom_stem,
        option_key=card.option.setting,
        option_default=effective_default,
        game_specific_options=option_gates.game_specific_options,
        per_core_options=option_gates.per_core_options,
    )
    stated = (*option_gates.caveats, *caveats)
    if value is None:
        return None, (*sources, provenance), stated
    if value not in card.option.values:
        return (
            None,
            (*sources, provenance),
            (
                *stated,
                Caveat(
                    CAVEAT_UNKNOWN_OPTION_VALUE,
                    f'core option {card.option.setting} = "{value}" is not a value the recorded mod '
                    f"behaviour of core {card.key!r} knows — whether mod loading is on is left "
                    "unstated rather than read as off",
                    {"core": card.key, "option_key": card.option.setting, "value": value},
                ),
            ),
        )
    return card.option.values[value], (*sources, provenance), stated


def _mod_trees(
    machine: Machine, *, card: ModCard, root: _SystemRoot
) -> tuple[tuple[ModTree, ...], tuple[str, ...], tuple[Caveat, ...]]:
    """One resolved tree per directory the card states, each with its own links.

    The link walk runs per tree rather than once, because the trees are separate
    directories an arrangement may wire separately — RetroDECK links FBNeo's
    three into its hub one by one — and a single physical directory could only
    ever be right for one of them.
    """
    trees: list[ModTree] = []
    sources: list[str] = []
    caveats: list[Caveat] = []
    for spec in card.trees:
        # A core card states fragments below the root RetroArch hands it; the
        # loader refuses a configured directory on this side outright.
        assert spec.subdir is not None
        directory = os.path.join(root.base, spec.subdir)
        physical: str | None = None
        if root.reachable and not root.needs:
            physical, link_caveats = _link_view(machine, directory)
            caveats.extend(link_caveats)
        trees.append(
            ModTree(dir=directory, keying=spec.keying, role=spec.role, physical_dir=physical)
        )
        named = f" ({spec.role})" if spec.role is not None else ""
        sources.append(
            f"mod card '{card.key}'{named}: the core reads mods from {spec.subdir!r} below that root"
        )
        if spec.keying is not None:
            sources.append(
                f"mod card '{card.key}'{named}: keyed by {spec.keying} — {spec.keying_citation}"
            )
    return tuple(trees), tuple(sources), tuple(caveats)


def _core_config_caveat(card: ModCard, *, root: str) -> Caveat | None:
    """The ini inside the core's own user tree that would answer the switch.

    A libretro core that ports a standalone emulator keeps that emulator's
    configuration inside the user directory it builds, so the setting is neither
    a core option nor a file atlas reads — which is exactly what
    ``emulator-config-unread`` says on a standalone row, arriving here on a core
    row for the first time. The path is resolved against the same root the trees
    hang off, so a caller is pointed at the file on *this* machine.
    """
    if card.config is None:
        return None
    path = os.path.join(root, card.config.path)
    return Caveat(
        CAVEAT_EMULATOR_CONFIG_UNREAD,
        f"whether core {card.key!r} has mod loading switched on is not established — it is not a "
        f"core option; the setting lives in {path}, an emulator configuration inside the user tree "
        "this core builds, which atlas does not read (standalone emulator configuration is its own "
        "roadmap block)",
        {"core": card.key, "config": path},
    )


def _soft_patching_caveat(card: ModCard, *, applies: bool | None) -> Caveat | None:
    """Does the frontend patch this core's content too, beside the trees above?

    Read rather than recorded: soft patching runs for exactly those cores whose
    content RetroArch loads into memory, and that is the reading
    :func:`_soft_patch_applies` already performs off the core's own ``.info``.
    A card flag would be world knowledge duplicating a live fact, and it would
    go stale the day a core's metadata changed.
    """
    if applies is not True:
        return None
    return Caveat(
        CAVEAT_SOFT_PATCHING_APPLIES,
        f"core {card.key!r} loads its content into memory, so RetroArch's own patching applies to it "
        "as well: a patch file beside the ROM is applied to the buffer before this core sees it, "
        "which is a different mechanism from the directories above and is answered by "
        "soft_patch_candidates()",
        {"core": card.key},
    )


def _retroarch_mod_location(machine: Machine, query: _SaveQuery) -> ModPlacement | Unresolved:
    """Where this core reads mods: the shared chain, then its mod card.

    The texture route's shape, over the same chain and the same roots, because
    the two families ask one question about two trees. What is this route's own
    is the plural: a card may state several directories that are different
    mechanisms, and each comes back as its own tree with its own keying and its
    own link walk.

    Two refusals, both typed: a core the machine established is not installed,
    and a core whose mod wiring atlas has not established — the second a
    statement about atlas and never the claim that the emulator has no mods.
    """
    chain = _read_chain(machine, query, SAVEFILE_KEYS)
    if chain.core.not_installed is not None:
        return chain.core.not_installed

    so_basename = os.path.basename(query.core_so) if query.core_so is not None else None
    card = lookup_mod_card(so_basename=so_basename, library_name=chain.core.library_name)
    if card is None:
        return Unresolved(
            UNRESOLVED_MOD_WIRING_UNESTABLISHED,
            f"where {so_basename or 'this emulator'} reads mods is not established — atlas carries "
            "no mod wiring for it, which says nothing about whether it has the feature: the "
            "packaged knowledge simply does not reach this core "
            "(docs/how-to-use.md, 'Where do mods go?')",
            {"core_so": so_basename} if so_basename is not None else {},
        )

    root = _card_root(root=card.root, chain=chain, query=query)
    trees, tree_sources, tree_caveats = _mod_trees(machine, card=card, root=root)
    enabled, enabled_sources, enabled_caveats = _mod_enabled(
        machine,
        card=card,
        chain=chain,
        query=query,
        core_version=chain.core.info.library_version if chain.core.info is not None else None,
    )
    applies: bool | None = None
    applies_sources: tuple[str, ...] = ()
    if query.core_so is not None:
        # The caveat below rests on a live reading of the core's .info, so that
        # reading is sourced here like every other stated fact. Its *caveats*
        # stay with the question that owns them: a .info nobody could read
        # leaves this answer stating nothing about soft patching, and a
        # degradation of a reading this answer does not make would be noise on
        # it — soft_patch_candidates() states them where they decide something.
        applies, applies_sources, _ = _soft_patch_applies(
            machine,
            sandbox=query.sandbox,
            parsed=chain.global_values,
            core_so=query.core_so,
        )
    return ModPlacement(
        trees=trees,
        needs=root.needs,
        enabled=enabled,
        sources=(
            *chain.sources,
            *root.sources,
            *tree_sources,
            f"mod card '{card.key}': {card.provenance}",
            *enabled_sources,
            *applies_sources,
        ),
        caveats=(
            *chain.caveats,
            *root.caveats,
            *enabled_caveats,
            *_optional(_core_config_caveat(card, root=root.base)),
            *_optional(_read_unestablished_caveat(card.key)),
            *_optional(_soft_patching_caveat(card, applies=applies)),
            *tree_caveats,
        ),
    )


def _duckstation_mod_placement(
    machine: Machine,
    *,
    card: StandaloneModCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    extra_caveats: tuple[Caveat, ...] = (),
) -> ModPlacement | Unresolved:
    """DuckStation's cheat tree: ``[Folders] Cheats``, below the root its launch picks.

    The reason this row needs a resolver rather than an XDG join is the same
    one that made the save card wrong before #250: DuckStation's DataRoot is
    the config home or the data home depending on the launch environment, so a
    card naming either would answer correctly on one arrangement and wrongly on
    the other. The key is then read the way every folder of this emulator is
    read — :func:`_duckstation_configured_directory`, the texture route's own
    reading.
    """
    spec = card.trees[0]
    setting = spec.directory
    if setting is None:
        raise ValueError(
            f"mod card {card.token!r} states no directory and this resolver reads one "
            "— the card and the code shipped out of step"
        )
    resolved = _duckstation_configured_directory(
        machine,
        token=card.token,
        setting=setting,
        homes=homes,
        sandbox=sandbox,
        extra_caveats=extra_caveats,
        reads="cheat files",
        named="cheats",
        switch="cheat loading",
    )
    if isinstance(resolved, Unresolved):
        return resolved
    directory, physical, caveats = resolved
    sources = [
        f"mod card '{card.token}': the directory is [{setting.section}] {setting.key} in the "
        f"emulator's own configuration — {setting.citation}",
        f"mod card '{card.token}': {card.provenance}",
    ]
    if spec.keying is not None:
        sources.insert(
            1, f"mod card '{card.token}': keyed by {spec.keying} — {spec.keying_citation}"
        )
    return ModPlacement(
        trees=(ModTree(dir=directory, keying=spec.keying, role=spec.role, physical_dir=physical),),
        needs=(),
        enabled=None,
        sources=tuple(sources),
        caveats=tuple(caveats),
    )


def _pcsx2_mod_placement(
    machine: Machine,
    *,
    card: StandaloneModCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    extra_caveats: tuple[Caveat, ...] = (),
) -> ModPlacement | Unresolved:
    """PCSX2's patch tree: ``[Folders] Patches``, read rather than assumed (#314).

    This row used to be a fixed ``patches`` join below the DataRoot, which was
    true only for as long as nobody set the key. Upstream reads it through
    ``LoadPathFromSettings`` with the compiled default ``patches``
    (Pcsx2Config.cpp:2288 with :2272-2278), exactly like the memory-card,
    texture, savestates and gamesettings directories — so a machine that sets
    it got a tree atlas never named, and confidently.

    The reading is the sibling routes' own, deliberately and not by accident:
    :func:`_pcsx2_folder_below_dataroot` for the three non-absolute outcomes
    (unset → the compiled default, present-but-empty → the DataRoot itself,
    relative → joined onto it), then the launch's sandbox for an absolute
    value, with the same untranslatable refusal the texture and memory-card
    directories give. Each PCSX2 route opens the ini itself, which is how the
    texture, save and states routes beside this one are written.

    ``enabled`` stays ``None``: the card names no switch, so nothing is read
    for one and ``emulator-config-unread`` says where one would live —
    ``[EmuCore] EnablePatches``, in the very file this reads for the directory.
    """
    spec = card.trees[0]
    setting = spec.directory
    if setting is None:
        raise ValueError(
            f"mod card {card.token!r} states no directory and this resolver reads one "
            "— the card and the code shipped out of step"
        )
    if card.settings is None:
        # A card MAY name no configuration file — that is the honest state for
        # an emulator whose switch nobody has found. It is not a state this
        # route can be in: the directory it answers with is read out of that
        # very file, so an unnamed one leaves nothing to read.
        raise ValueError(
            f"mod card {card.token!r} names no configuration file and this resolver reads its "
            "directory out of one — the card and the code shipped out of step"
        )
    settings = emulator_settings.settings_file(card.token, card.settings)
    data_root = homes.emulator_root(settings.bases[0], card.token)
    ini_path = settings.only(
        config_home=homes.base("config"), data_home=homes.base("data"), flatpak=homes.flatpak
    )
    result = machine.read_text(ini_path)
    if result.status not in (READ_OK, READ_MISSING):
        return Unresolved(
            UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
            f"PCSX2's configuration ({ini_path}) exists and could not be read — where it "
            "reads patches from is unknowable here",
            {"emulator": card.token, "config": ini_path},
        )
    values = qt_ini.values(result.text) if result.status == READ_OK and result.text else {}
    # The key the way the emulator matches it (#295): CSimpleIniA is ASCII
    # case-insensitive, so a case-variant spelling governs here as it does
    # in the running emulator (:func:`atlas.qt_ini.simpleini_value`).
    raw_dir, dir_spelled = _simpleini_value(values, setting.section, setting.key)
    resolved, _ = _pcsx2_folder_below_dataroot(
        raw_dir,
        key=setting.key,
        default=setting.default,
        default_citation=setting.citation,
        data_root=data_root,
        spelled=dir_spelled,
    )
    if resolved is None:
        assert raw_dir is not None  # only an absolute value leaves the helper unresolved
        host = sandbox.host(setting.key, raw_dir)
        if host.path is None:
            return Unresolved(
                UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE,
                f"the patches directory PCSX2's configuration names ({raw_dir!r}) has no "
                f"spelling on this host — {ini_path} read fine, and nothing this answer "
                "could anchor at",
                {"emulator": card.token, "config": ini_path, "path": raw_dir},
            )
        directory = host.path
    else:
        directory = resolved
    physical, link_caveats = _link_view(machine, directory)
    caveats: list[Caveat] = [*extra_caveats, *link_caveats]
    caveats.append(
        Caveat(
            CAVEAT_EMULATOR_CONFIG_UNREAD,
            f"whether {card.token} has patch loading switched on is not established — the "
            f"setting lives in {ini_path}, which this answer reads for the directory and not "
            "for the switch, because the card states none",
            {"emulator": card.token, "config": ini_path},
        )
    )
    sources = [
        f"mod card '{card.token}': the directory is [{setting.section}] {setting.key} in the "
        f"emulator's own configuration — {setting.citation}",
        f"mod card '{card.token}': {card.provenance}",
    ]
    if spec.keying is not None:
        sources.insert(
            1, f"mod card '{card.token}': keyed by {spec.keying} — {spec.keying_citation}"
        )
    return ModPlacement(
        trees=(ModTree(dir=directory, keying=spec.keying, role=spec.role, physical_dir=physical),),
        needs=(),
        enabled=None,
        sources=tuple(sources),
        caveats=tuple(caveats),
    )


# Standalone mod cards whose tree hangs off a configuration value rather than a
# fixed XDG join. Keyed by token like the texture resolvers beside them, and a
# card stating a directory setting without one here fails loudly rather than
# answering a default nobody read.
_STANDALONE_MOD_RESOLVERS = {
    "DUCKSTATION": _duckstation_mod_placement,
    "PCSX2": _pcsx2_mod_placement,
}


def _standalone_mod_placement(
    machine: Machine,
    *,
    card: StandaloneModCard,
    homes: _XdgHomes,
    sandbox: _Sandbox,
    extra_caveats: tuple[Caveat, ...] = (),
) -> ModPlacement | Unresolved:
    """Where a standalone emulator reads mods — an XDG join, then the links.

    The texture family's standalone answer, with one difference that is evidence
    rather than design: a card may name **no** configuration file. Naming one
    says "this is where the switch would be", and for one emulator nobody has
    established that a switch exists at all — not a core option, not a CLI flag,
    nothing. That row states ``enabled`` as unanswered and stays silent about
    where to look, which is a weaker claim than ``emulator-config-unread`` and
    the only honest one available.
    """
    if card.configured:
        resolver = _STANDALONE_MOD_RESOLVERS.get(card.token)
        if resolver is None:
            raise ValueError(
                f"standalone mod card {card.token!r} states a directory setting but has no "
                "resolver registered — the card and the code shipped out of step"
            )
        return resolver(
            machine, card=card, homes=homes, sandbox=sandbox, extra_caveats=extra_caveats
        )
    if card.base is None:
        raise ValueError(
            f"mod card {card.token!r} states no base and this resolver opens one below "
            "it — the card and the code shipped out of step"
        )
    trees: list[ModTree] = []
    sources: list[str] = []
    caveats: list[Caveat] = [*extra_caveats]
    for spec in card.trees:
        assert spec.subdir is not None  # no configured tree reaches this branch
        directory = os.path.join(homes.emulator_root(card.base, card.token), spec.subdir)
        physical, link_caveats = _link_view(machine, directory)
        caveats.extend(link_caveats)
        trees.append(
            ModTree(dir=directory, keying=spec.keying, role=spec.role, physical_dir=physical)
        )
        sources.append(
            f"mod card '{card.token}': the emulator reads mods from {spec.subdir!r} below "
            f"its own {emulator_settings.user_directory(card.token, flatpak=homes.flatpak)!r} "
            f"directory in the XDG {card.base} home — {card.provenance}"
        )
        if spec.keying is not None:
            sources.append(
                f"mod card '{card.token}': keyed by {spec.keying} — {spec.keying_citation}"
            )
    if card.settings is not None:
        config_path = emulator_settings.settings_file(card.token, card.settings).only(
            config_home=homes.base("config"), data_home=homes.base("data"), flatpak=homes.flatpak
        )
        caveats.append(
            Caveat(
                CAVEAT_EMULATOR_CONFIG_UNREAD,
                f"whether {card.token} has mod loading switched on is not established — the setting "
                f"lives in {config_path}, a configuration of the emulator's own that atlas does not "
                "read (standalone emulator configuration is its own roadmap block)",
                {"emulator": card.token, "config": config_path},
            )
        )
    else:
        sources.append(
            f"mod card '{card.token}': no switch is established for this emulator — neither a core "
            "option nor a setting anyone has found, so whether loading is on is not stated"
        )
    # The same Load directory the texture answer hangs below: one per-game key
    # re-points it and the graphics-mod tree moves with it. Empty for every
    # other token.
    caveats.extend(
        _dolphin_game_settings_caveats(
            machine,
            token=card.token,
            homes=homes,
            keys=_DOLPHIN_LOAD_LAYER_KEYS,
            governs="the Load directory the mod tree hangs below",
        )
    )
    return ModPlacement(
        trees=tuple(trees),
        needs=(),
        enabled=None,
        sources=tuple(sources),
        caveats=tuple(caveats),
    )


def _standalone_mod_unresolved(spec: EmulatorSpec) -> Unresolved:
    """The refusal for a standalone entry no packaged mod card covers.

    The same code the save routes answer every standalone entry with, and for
    the reason the texture family gives: what is missing is that nobody reads
    this emulator's own configuration, and for these emulators the mod
    directory is *in* that configuration rather than at a default the emulator
    opens — RetroDECK writes MAME's ``pluginspath`` and ``homepath`` into
    ``mame.ini``. An emulator whose mods live at its own default answers
    instead, and the split between the two is evidence, not policy.
    """
    return Unresolved(
        UNRESOLVED_STANDALONE,
        f"where standalone emulator {spec.label!r} ({spec.system}) reads mods is not resolvable yet "
        "— its mod directory is named in a configuration of its own, and reading those is the "
        "standalone roadmap block (ROADMAP.md)",
        {"label": spec.label, "system": spec.system},
    )


# What naming no core costs the soft-patching question — the third entry beside
# NO_CORE_FOR_SAVES and NO_CORE_FOR_STATES, and the smallest of the three: the
# candidate files follow from the content path alone, so a nameless core costs
# exactly one field.
NO_CORE_FOR_SOFT_PATCHING = (
    "no core given — the candidate files below are the content's own and stand either way, but "
    "whether patching runs at all is a fact about the core (it patches only content it loads into "
    "memory), so 'applies' is left unanswered rather than assumed"
)

# The ``.info`` key that declares whether a core is handed a path instead of the
# content's bytes. RetroArch itself does not read this key — the gate is the
# core's own ``retro_system_info.need_fullpath`` (``task_content.c:744-745``) —
# so what atlas reads here is the *declaration* beside the core, spelled the way
# the metadata spells it (``needs_fullpath``, not the struct field's
# ``need_fullpath``: 174 of the 292 ``.info`` files a stock RetroDECK ships state
# it, 87 each way).
INFO_NEEDS_FULLPATH = "needs_fullpath"


def _soft_patch_applies(
    machine: Machine,
    *,
    sandbox: _Sandbox,
    parsed: Mapping[str, str],
    core_so: str,
) -> tuple[bool | None, tuple[str, ...], tuple[Caveat, ...]]:
    """Does the frontend patch what this core loads — read off the core's own ``.info``?

    RetroArch patches the content **buffer**, so it patches only content it
    loads into memory, which is every core that does not need a full path
    (``task_content.c:1465-1484``). What the machine states about that is the
    ``.info``'s ``needs_fullpath``, read here the same way the savestate route
    reads that file's ``savestate`` declaration — same file, same directory key,
    same three ways to fail.

    It is a declaration and the answer says so: RetroArch never reads this key
    (nothing in ``core_info.c`` looks it up), and the flag that decides is the
    one the *core* reports when it is loaded. The two agree wherever the
    metadata is current, and where they disagree the core wins — which is why a
    stated ``applies`` is worth having and worth qualifying, and why a ``.info``
    that says nothing leaves it unanswered instead of falling back to a default.
    """
    info_dir, dir_caveats = _cfg_directory(sandbox, parsed, "libretro_info_path")
    if info_dir is None:
        return None, (), (*dir_caveats, _info_path_unresolved_for_patching())
    info_path = os.path.join(info_dir, os.path.basename(core_so).removesuffix(".so") + ".info")
    read = machine.read_text(info_path)
    if read.text is None:
        return None, (), (_core_info_unreadable_for_patching(core_so, read.status),)
    declared = parse_core_info(read.text).get(INFO_NEEDS_FULLPATH)
    if declared is None or (needs_fullpath := cfg_bool(declared)) is None:
        # Read, and it said nothing this vocabulary can act on. Stated as the
        # honest None with its provenance rather than as a caveat: the file was
        # reachable and simply carries no such declaration, which is the state
        # 118 of the 292 shipped .info files are in.
        return (
            None,
            (
                f"{info_path}: no {INFO_NEEDS_FULLPATH} declaration this reading can use"
                + (f' (states "{declared}")' if declared is not None else ""),
            ),
            (),
        )
    return (
        not needs_fullpath,
        (
            f'{info_path}: {INFO_NEEDS_FULLPATH} = "{declared}" — the content is '
            f"{'handed to the core as a path' if needs_fullpath else 'loaded into memory'}, so the "
            f"frontend {'never patches it' if needs_fullpath else 'patches it before the core sees it'}",
        ),
        (),
    )


def _info_path_unresolved_for_patching() -> Caveat:
    """``libretro_info_path`` names no readable directory, so no ``.info`` was read."""
    return Caveat(
        CAVEAT_INFO_PATH_UNRESOLVED,
        "libretro_info_path does not resolve to a readable directory on this machine — whether this "
        "core loads its content into memory, and so whether the candidates below are ever applied, "
        "could not be established",
    )


def _core_info_unreadable_for_patching(core_so: str, status: ReadStatus) -> Caveat:
    """The core's ``.info`` is there in name only — the same code the other routes state."""
    return Caveat(
        CAVEAT_CORE_INFO_UNREADABLE,
        f"{os.path.basename(core_so)}'s .info could not be read ({status}) — whether this core "
        "loads its content into memory is unknown, so whether the candidates below are ever "
        "applied is unstated rather than assumed",
        {"core_so": os.path.basename(core_so), "status": status},
    )


def _patch_formats(
    build: SoftPatchBuild | None, *, arrangement_version: str | None
) -> tuple[Mapping[str, bool], tuple[str, ...], tuple[Caveat, ...]]:
    """Which formats this build attempts — from the packaged record, or unestablished.

    The record is read by installation kind and names no arrangement in code, so
    the day someone reads another distribution's binary the claim arrives as a
    data change (the pattern ``arrangement_evidence.json`` and the rule cards
    both follow).

    The version comparison is the texture family's, verbatim in intent: a claim
    about a build is not carried across a version nobody re-examined, and it
    needs both sides to speak — a machine that states no version is not
    compared, and that silence means *no drift established*, not *no drift*.
    """
    if build is None:
        return (
            {},
            (),
            (
                Caveat(
                    CAVEAT_PATCH_FORMATS_UNESTABLISHED,
                    "which patch formats this RetroArch attempts is not established — patching and "
                    "its .xdelta applier are compile-time flags (Makefile.common:260-267) and "
                    "nothing on a running machine states how they were set, so each candidate "
                    "below is listed without a claim that this build tries it",
                    {"formats": ",".join(PATCH_FORMATS)},
                ),
            ),
        )
    caveats: list[Caveat] = []
    if arrangement_version is not None and arrangement_version != build.verified_arrangement:
        caveats.append(
            Caveat(
                CAVEAT_UNVERIFIED_VERSION,
                f"which formats this build attempts was established against "
                f"{build.verified_arrangement}, and this machine states {arrangement_version} — a "
                "build is exactly what could add or drop an applier, so the claim is not carried "
                "across the difference unexamined",
                {
                    "verification": "drifted",
                    "arrangement_verified": build.verified_arrangement,
                    "arrangement_live": arrangement_version,
                },
            )
        )
    return (
        build.attempts(),
        (f"soft-patching record '{build.kind}': {build.citation}",),
        tuple(caveats),
    )


def _retroarch_soft_patch_candidates(
    machine: Machine, query: _SaveQuery, *, build: SoftPatchBuild | None
) -> SoftPatchAnswer | Unresolved:
    """Which files RetroArch would patch this content with — arithmetic, then two readings.

    Deliberately **not** built on the shared chain the other three routes read.
    That chain resolves a save layout — roots, sorting stages, override layers —
    and none of it reaches this question: the candidate files sit beside the
    content, so the only things to read are the core (is it installed, does it
    load content into memory) and the one cfg key that says where ``.info``
    files live. Reading the save layout to answer a question it cannot move
    would put its degradations on an answer they say nothing about.

    A core the machine established is not installed ends the question the way it
    ends the other three. The candidates would still be true — they are the
    content's own — but ``applies`` is then a question about a core that cannot
    run, and one refusal code across the family is worth more to a client than
    an answer that is half about a core it does not have.
    """
    core = _identify_core(
        machine,
        core_so=query.core_so,
        core_path_resolver=query.core_path_resolver,
        no_core_message=NO_CORE_FOR_SOFT_PATCHING,
    )
    if core.not_installed is not None:
        return core.not_installed

    parsed = parse_cfg_text(query.global_text) if query.global_text is not None else {}
    caveats = [*query.extra_caveats, *core.caveats]
    sources = [*query.extra_sources, *core.sources]

    applies: bool | None = None
    if query.core_so is not None:
        applies, applies_sources, applies_caveats = _soft_patch_applies(
            machine, sandbox=query.sandbox, parsed=parsed, core_so=query.core_so
        )
        sources.extend(applies_sources)
        caveats.extend(applies_caveats)

    attempted, format_sources, format_caveats = _patch_formats(
        build, arrangement_version=query.arrangement_version
    )
    sources.extend(format_sources)
    caveats.extend(format_caveats)

    basename = content_basename(query.content_path) if query.content_path else ""
    # "Names a file" is the *last component* being non-empty, not the basename
    # being non-empty: ``/roms/psx/Game/`` keeps its trailing slash all the way
    # through the path math and would compose ``/roms/psx/Game/.ips`` — a
    # dotfile in a directory nobody named. It is the same test the save routes
    # make on their rom_stem, and the same caveat.
    if not os.path.basename(basename):
        caveats.append(_unnamed_content_caveat(query.content_path or ""))
        basename = ""
    else:
        sources.append(
            f"content {query.content_path!r}: RetroArch names patches after "
            f"{basename!r} — the content path with its last extension truncated, and for content "
            "inside an archive the entry's own name in the archive's directory "
            "(runloop.c:8673-8713, then runloop.c:5196-5253)"
        )
        sources.append(
            "attempt order ips -> bps -> ups -> xdelta, first hit wins, then indexed continuations "
            "<name>1..<name>9 stopping at the first gap (task_patch.c:1071-1075, :1121-1147); the "
            "patch is applied to the in-memory buffer and never to the file on disk "
            "(task_patch.c:872-879)"
        )
    return SoftPatchAnswer(
        candidates=build_soft_patch_candidates(content_basename=basename, attempted=attempted),
        applies=applies,
        sources=tuple(sources),
        caveats=tuple(caveats),
    )


def _cfg_directory(
    sandbox: _Sandbox, parsed: Mapping[str, str], key: str
) -> tuple[str | None, tuple[Caveat, ...]]:
    """Resolve a cfg directory key to a host directory that exists, or ``None``.

    The configured value is written in the emulator's own spelling, which inside
    a Flatpak means the sandbox's (``/app/...``, ``/var/config/...``); a caller
    running outside it needs the host location instead. ``None`` means unset,
    reset, unresolvable, or not an existing directory — one honest miss, never a
    path handed out as if it were usable — and a sandbox path with no host
    location comes with the caveat that says so.
    """
    resolved = sandbox.cfg_path(key, parsed.get(key))
    if resolved is None:
        return None, ()
    if resolved.path is None or sandbox.machine.path_kind(resolved.path) != KIND_DIRECTORY:
        return None, resolved.caveats
    return resolved.path, ()


def _firmware_root_missing(root: str) -> Caveat:
    """The whole firmware root is gone — one fact, not one per declared file."""
    return Caveat(
        CAVEAT_FIRMWARE_ROOT_MISSING,
        f"the system_directory {root} is not an existing directory — every declared file below is "
        "missing because the whole firmware root is gone, not one file at a time",
        {"path": root},
    )


def _retroarch_firmware_context(
    *,
    sandbox: _Sandbox,
    global_text: str | None,
    cfg_label: str,
    retroarch_config_dir: str,
    findings: tuple[Caveat, ...],
    arrangement_version: str | None,
    extra_sources: tuple[str, ...] = (),
    standalone_homes: _XdgHomes | None = None,
    standalone_sandbox: _Sandbox | None = None,
) -> FirmwareContext:
    """One live read of everything a firmware answer needs, for any arrangement.

    One path for every arrangement (D2): RetroDECK, EmuDeck and a bare
    RetroArch differ only in which cfg is read and whether its paths need
    sandbox translation. The three keys are read independently and may point
    anywhere: ``libretro_info_path`` names the ``.info`` files that declare
    what the installed cores want, ``libretro_directory`` says which of those
    cores are actually installed, and ``system_directory`` is the root the
    declared paths are relative to — the same directory RetroArch hands cores
    when they look their firmware up. RetroDECK happens to collapse the first
    two into one directory; nothing here assumes it.

    *extra_sources* are the caller's own statements about how this cfg is
    read — the RetroDECK handle names the override file whose HOME decides
    the ``~`` expansion here — and they lead the sources the reads below
    append.

    *standalone_sandbox* is how a standalone emulator's own absolute config
    values read from this host — the same map its save route translates
    through, so the two routes cannot disagree about where a configured
    directory lands. It rides beside *standalone_homes* because the two are
    one fact about the same launch, and an arrangement that establishes no
    homes resolves no card that would ask.
    """
    machine = sandbox.machine
    # The dropped lines are read here, not only the values: once an absent key
    # resolves silently to the platform default, a line the parser refused
    # looks exactly like a key nobody wrote — and the user did write it. The
    # card route states the same fact for the same reason.
    read = parse_cfg(global_text) if global_text is not None else ParsedCfg({})
    parsed = read.values
    # The installation's own health leads: whether the arrangement is broken is
    # the most general thing about any answer, so it stands before what this
    # particular read could not resolve. The caller derives the findings from
    # the same reads it passed *global_text* out of.
    caveats: list[Caveat] = list(findings)
    sources: list[str] = list(extra_sources)

    raw_system = parsed.get("system_directory")
    configured_system = sandbox.cfg_path("system_directory", raw_system) if raw_system is not None else None
    root = configured_system.path if configured_system is not None else None
    caveats.extend(
        _ignored_caveats(
            tuple(
                IgnoredSetting(IGNORED_LINE_DROPPED, cfg_label, line.key, line.line)
                for line in read.dropped
                if line.key == "system_directory"
            )
        )
    )
    if raw_system is None:
        # Absent is not unset-and-unknown: RetroArch seeded the platform default
        # before it read a line of config, so this route resolves it exactly as
        # the card route does and scans there. A line the parser refused lands
        # here too, and is stated above rather than resolving in silence.
        root = _platform_system_dir(retroarch_config_dir)
        sources.append(PLATFORM_SYSTEM_DIR_SOURCE)
        if machine.path_kind(root) != KIND_DIRECTORY:
            caveats.append(_firmware_root_missing(root))
    elif configured_system is None:
        # Set to blank or the literal "default": the setting is empty, and what
        # the core is handed then depends on the run, not on the config.
        caveats.append(
            Caveat(
                CAVEAT_SYSTEM_DIRECTORY_CLEARED,
                f'system_directory = "{raw_system}" clears the setting, so what a core is handed as '
                "its system directory is decided per run: with content loaded RetroArch passes the "
                "content's own directory (runloop.c:1958-1997), and no content is named here — so "
                "there is no one root to check firmware against",
                {"value": raw_system},
            )
        )
    elif root is None:
        caveats.extend(configured_system.caveats)
    else:
        sources.append(f'{cfg_label}: system_directory = "{raw_system}"{configured_system.note}')
        if machine.path_kind(root) != KIND_DIRECTORY:
            caveats.append(_firmware_root_missing(root))

    info_dir, info_caveats = _cfg_directory(sandbox, parsed, "libretro_info_path")
    core_dir, core_dir_caveats = _cfg_directory(sandbox, parsed, "libretro_directory")
    caveats.extend((*info_caveats, *core_dir_caveats))
    cores: tuple[CoreDeclarations, ...] = ()
    cores_listed = False
    if info_dir is None:
        caveats.append(
            Caveat(
                CAVEAT_INFO_PATH_UNRESOLVED,
                "libretro_info_path does not resolve to a readable directory on this machine — the cores' "
                "own firmware declarations cannot be read, so nothing below is declared",
            )
        )
    else:
        sources.append(f"core declarations read live from {info_dir} (.info firmwareN_path)")
        if core_dir is None:
            caveats.append(
                Caveat(
                    CAVEAT_CORE_DIR_UNRESOLVED,
                    "libretro_directory does not resolve to a readable directory — which cores are actually "
                    "installed cannot be checked, so declarations by cores this installation does not ship "
                    "are included",
                )
            )
        else:
            sources.append(f"declarations limited to cores installed in {core_dir}")
        enumeration = read_core_declarations(machine, info_dir, core_dir=core_dir)
        cores = enumeration.cores
        cores_listed = enumeration.listed
        caveats.extend(
            Caveat(
                CAVEAT_CORE_ENUMERATION_INCOMPLETE,
                f"{unreadable} could not be listed (permissions or an I/O failure), so which cores are "
                "installed is unknown — the cores below are the ones atlas could see, not the set this "
                "installation ships",
                {"path": unreadable},
            )
            for unreadable in enumeration.unreadable
        )

    return FirmwareContext(
        root=root,
        cores=cores,
        hashes=load_hashes(),
        # Whether the enumeration happened is now the seam's answer, not a
        # guess from the list being empty. That guess was wrong in the safe
        # direction — a genuinely empty core directory read as "nobody looked",
        # so an installation that ships no cores could never say so — and the
        # case it protected against, a directory that resolves but cannot be
        # listed, is stated above by its own caveat.
        cores_read=info_dir is not None and cores_listed,
        sources=tuple(sources),
        caveats=tuple(caveats),
        arrangement_version=arrangement_version,
        standalone_data_home=standalone_homes.data if standalone_homes is not None else None,
        standalone_config_home=standalone_homes.config if standalone_homes is not None else None,
        standalone_flatpak=standalone_homes.flatpak if standalone_homes is not None else None,
        standalone_sandbox=standalone_sandbox,
        standalone_xdg_pinned=standalone_homes is not None and standalone_homes.xdg_pinned,
    )


class _FirmwareQueries:
    """The four firmware entry points, over one live context per query.

    Every handle answers the same four questions; only the way its context is
    assembled differs. An installation whose frontend catalogue can enumerate a
    system's emulators overrides :meth:`firmware_for_system` to pass it.
    """

    kind: str
    _machine: Machine

    def _read_firmware_context(self) -> FirmwareContext:
        raise NotImplementedError  # pragma: no cover - every handle supplies one

    def _stated(self, context: FirmwareContext) -> FirmwareContext:
        """A context with what atlas has established about this arrangement.

        Every firmware answer copies the context's caveats into itself, so
        stating the evidence once here states it on all four. The installation's
        health findings are already at the front of those caveats: each handle
        derives them from the very reads it built the context out of, because a
        second read for them could see a different revision of the same file
        than the answer did (REVIEW M4).

        The version the evidence is weighed against comes off the context for
        the same reason — it is what the handle's own read saw, not a fresh
        one.
        """
        return _dc_replace(
            context,
            caveats=(
                *context.caveats,
                *arrangement_caveats(self.kind, observed_version=context.arrangement_version),
            ),
        )

    def _firmware_context(self) -> FirmwareContext:
        """The handle's live read, stated — the context all four questions answer from."""
        return self._stated(self._read_firmware_context())

    def firmware_for_core(self, core_so: str, *, verify: bool = False) -> FirmwareAnswer:
        """Does *core_so* need firmware, where does each file go, and is it there?"""
        return _resolve_for_core(self._machine, self._firmware_context(), core_so=core_so, verify=verify)

    def firmware_for_system(self, system: str, *, verify: bool = False) -> FirmwareAnswer:
        """Which emulators can run *system*, and what does each of them want?"""
        return _resolve_for_system(self._machine, self._firmware_context(), system=system, verify=verify)

    def firmware_inventory(self, *, verify: bool = False) -> FirmwareAnswer:
        """Every installed core's firmware, plus what is lying around unclaimed."""
        return _resolve_inventory(self._machine, self._firmware_context(), verify=verify)

    def identify_firmware(
        self, *, md5: str | None = None, sha1: str | None = None, size: int | None = None
    ) -> FirmwareIdentification:
        """What is this content, and which requirements here does it satisfy?"""
        return _resolve_identification(
            self._machine, self._firmware_context(), md5=md5, sha1=sha1, size=size
        )


def _card_files(files: tuple[str, ...], rom_stem: str | None) -> tuple[str, ...] | None:
    """Substitute the ``<rom_stem>`` hole in a card's declared file list.

    Returns ``None`` when a template file cannot be filled (no content given) —
    the file set is then honestly unknown rather than a template guess. Holes
    the resolver is not the one to fill (``<save_id>``) stay in the name and
    reach the caller through ``needs``.
    """
    resolved: list[str] = []
    for name in files:
        if TEMPLATE_ROM_STEM in name:
            if rom_stem is None:
                return None
            name = name.replace(TEMPLATE_ROM_STEM, rom_stem)
        resolved.append(name)
    return tuple(resolved)


_JSON_TYPE_NAMES: Mapping[type, str] = {
    bool: "a boolean",
    int: "a number",
    float: "a number",
    list: "a list",
    dict: "an object",
    type(None): "null",
}


def _json_type_name(value: object) -> str:
    """What a JSON value is, for a message that says why the marker is refused."""
    return _JSON_TYPE_NAMES.get(type(value), type(value).__name__)


# The ``retrodeck.json`` path keys atlas reads — the ones :meth:`RetroDeck._config_path`
# is asked for. A key read through it belongs here, because this is the list the
# type check below is scoped to.
#
# ``roms_path`` left when the ROM directory moved to ES-DE's own ``ROMDirectory``:
# nothing here reads it any more, so checking it would be atlas reporting on a
# key it does not use — the same thing the scoping rule below refuses to do for
# every other key RetroDECK writes.
_MARKER_PATH_KEYS = ("rd_home_path", "saves_path", "bios_path")


def _malformed_marker_paths(config: Mapping[str, Any]) -> tuple[str, str] | None:
    """A ``paths`` value atlas reads that is not a path string — ``(key, complaint)``.

    ``retrodeck.json`` is editable and its ``paths`` section drives every read
    that follows, so the type is checked here, at the one place the marker is
    read, rather than at each use: a non-string leaking out of
    :meth:`RetroDeck.root` reaches ``os.stat``, which reads an ``int`` as a
    *file descriptor* and so can answer for something that is not even a path.

    Scoped to :data:`_MARKER_PATH_KEYS`, because atlas reports on what it reads.
    A value under some other key says nothing about the reads atlas performs,
    and calling the marker broken over it would be a claim about who wrote the
    file rather than a reading of the machine — a future RetroDECK that nests
    something new under ``paths`` would take a healthy installation down to its
    fallback roots. A ``paths`` that is not an object at all is different: no
    key can be read from it, so every read is affected.
    """
    if "paths" not in config:
        return None
    # Absent and null are different states: a marker that omits the section
    # says nothing and the fallbacks answer, while one that writes ``null``
    # there has written a section no key can be read from.
    paths: object = config["paths"]
    if not isinstance(paths, dict):
        return "paths", f"is {_json_type_name(paths)}, not an object of path strings"
    for key in _MARKER_PATH_KEYS:
        if key not in paths:
            continue
        value: object = paths[key]
        if not isinstance(value, str):
            return f"paths.{key}", f"is {_json_type_name(value)}, not a path string"
    return None


def _marker_version(config: Mapping[str, Any]) -> str | None:
    """The version a ``retrodeck.json`` states about itself — ``None`` for none.

    One definition of "this marker names a version", used by both checks that
    ask: the per-card version comparison and the arrangement-level one. Two
    readings of the same key could disagree about whether a machine stated
    anything, and then one caveat would report drift where the other reported
    silence.

    Empty is *unstated*, because that is RetroDECK's own spelling for it: the
    shipped default config carries ``"version": ""`` and the first run fills it
    in (``libexec/global.sh``, ``conf_write`` in ``libexec/other_functions.sh``,
    RetroDECK 0.10.9b). A non-string is neither — :meth:`RetroDeck._read_marker`
    states that as a health finding, and nothing is compared against it.
    """
    value: object = config.get("version")
    return value if isinstance(value, str) and value else None


def _normalized_path(path: str) -> str:
    """One spelling for two paths that name the same place, for comparison only.

    Lexical: separators, ``.``, ``..`` and repeated slashes are folded, links
    are not followed. Nothing here touches the machine.
    """
    return os.path.normpath(path.replace("\\", "/"))


def _match_per_game(
    selections: GamelistSelections, content_path: str, *, system_roms_dir: str
) -> str | None:
    """Match a content path against per-game ``altemulator`` entries, anchored.

    Gamelist paths are relative to the system's own ROM directory
    (``./Name.ext``, or ``./Folder`` for multi-disc directory entries) and may
    contain subdirectories — ``./USA/Game.iso`` and ``./Japan/Game.iso`` are
    distinct games. So each entry is resolved against *system_roms_dir* and
    compared as a whole path: an unanchored suffix match hands one game's
    selection to every same-named file at any depth below, and the live psx
    gamelist has exactly that shape — a root-level ``./<name>.m3u`` carrying the
    override next to a folder of the same name holding a second ``<name>.m3u``
    that carries none.

    A directory entry matches the files inside it, which is what makes the
    multi-disc convention work; an entry naming the content file itself is the
    more specific statement and wins over a directory entry that also covers it.

    **The comparison is lexical** (:func:`_normalized_path`): a content path
    spelled through a symlink — and RetroDECK's own tree uses symlinks liberally
    — names the same file for the kernel but not for this match, so the answer
    falls back to the per-system selection. Resolving would cost a read per
    entry per query, which is exactly what the one-read-per-source rule exists
    to prevent, so the limit is stated (here and in the developer guide) rather
    than paid for: name the content the way it lies under the ROM directory.
    """
    content = _normalized_path(content_path)
    parent = os.path.dirname(content)
    directory_label: str | None = None
    for rel_path, label in selections.per_game.items():
        entry = _normalized_path(os.path.join(system_roms_dir, rel_path))
        if content == entry:
            return label
        if directory_label is None and parent == entry:
            directory_label = label
    return directory_label


@runtime_checkable
class _CatalogueHost(Protocol):
    """What an entry needs back from the installation that produced it.

    Narrow on purpose: an entry asks its installation for placements and
    nothing else, so the type it is bound to is those methods rather than a
    concrete handle. It used to be ``RetroDeck``, which made every entry — and
    so the catalogue answer itself — RetroDECK's to hand out.
    """

    def entry_savefile_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> SavefilePlacement | Unresolved: ...

    def entry_savestate_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> SavestatePlacement | SavestateAbsence | Unresolved: ...

    def entry_texture_pack_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> TexturePlacement | Unresolved: ...

    def entry_mod_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> ModPlacement | Unresolved: ...

    def standalone_firmware_token(self, command: str) -> str | None:
        """The emulator identity *command* states, for the firmware seam.

        Arrangement knowledge on purpose: RetroDECK's commands carry the
        ``%EMULATOR_…%`` token, EmuDeck's run launcher scripts — each handle
        answers with its own reading, and ``None`` where the command
        identifies nothing atlas can act on.
        """
        ...

    def standalone_firmware_homes(self, command: str) -> "_XdgHomes | None":
        """Per-entry XDG bases where this launch's binary reads its own trees.

        ``None`` means the arrangement's own standalone bases govern — the
        pair the firmware context carries. A value means the entry's launch
        picks a binary whose trees hang elsewhere (EmuDeck's flatpak variant
        reads ``~/.var/app/<id>``, not the host's XDG tree).
        """
        ...


@dataclass(frozen=True, slots=True)
class CatalogueAnswer:
    """Which emulators a frontend catalogue declares for one system.

    An answer object rather than a bare tuple for the reason every other answer
    here is one: empty is not self-explaining. The catalogue may declare no
    emulator for this system, or the arrangement may have no catalogue, or its
    catalogue may not have been readable — three different facts that a tuple
    spells the same way. ``caveats`` carries which, ``sources`` says what was
    read to find out.
    """

    entries: tuple[EmulatorEntry, ...] = ()
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


@dataclass(frozen=True, slots=True)
class SystemsAnswer:
    """Every system a frontend catalogue declares, and what was read to say so.

    The same empty-is-ambiguous problem as :class:`CatalogueAnswer`, one level
    up: a catalogue that could not be read lists no systems, and so does one
    that genuinely declares none.
    """

    systems: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


# The three statuses a platform question states about a system (issue #68).
# "declared" — this installation's own systems answer lists it. "disabled" —
# the catalogue's text carries it only inside an XML comment: present and
# deliberately off, which is a different fact than never shipped. "absent" —
# the vocabulary knows the system and this installation does not have it.
# They never collapse, because the consumer's next step differs for each:
# place content, ask the user to enable, or don't place it here at all.
PLATFORM_STATUS_DECLARED = "declared"
PLATFORM_STATUS_DISABLED = "disabled"
PLATFORM_STATUS_ABSENT = "absent"

# Where a statement's platform tags came from. "catalogue" is a read of this
# machine — the system's own <platform> text (a commented block included).
# "vocabulary" is the snapshot column of the stated build (atlas.systems):
# what backs an absent system, and a declared one whose catalogue is sealed —
# world knowledge, marked as such per the boundary rule.
PLATFORM_TAGS_CATALOGUE = "catalogue"
PLATFORM_TAGS_VOCABULARY = "vocabulary"


@dataclass(frozen=True, slots=True)
class PlatformSystemMatch:
    """One system answering to a public platform id, and how firmly it is here.

    ``platforms`` is the system's whole tag list (not just the matching tag) —
    a consumer deciding between two matches wants to see that ``naomi`` is an
    ``arcade`` system while ``dreamcast`` is the family's own platform.
    """

    system: str
    status: str
    platforms: tuple[str, ...]
    tags_source: str


@dataclass(frozen=True, slots=True)
class PlatformSystemsAnswer:
    """Which of this installation's systems answer to a public platform id.

    ``platforms`` is what the crosswalk resolved the id to — empty exactly
    when the ``platform-unmapped`` caveat states why. ``matches`` is ordered:
    declared systems first, then disabled, then absent, each alphabetical.
    An empty match list under resolved platforms is a statement about this
    machine — the platform is real and nothing here answers to it.
    """

    vocabulary: str
    value: str
    platforms: tuple[str, ...] = ()
    matches: tuple[PlatformSystemMatch, ...] = ()
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


@dataclass(frozen=True, slots=True)
class SystemPlatformsAnswer:
    """One system's platform tags and their public identities, status-qualified.

    ``identities`` carries one entry per tag the crosswalk knows; a tag it
    does not know states ``platform-unknown`` instead, and the ``ignore``
    sentinel states ``platform-scraping-ignored`` — so the identity list, the
    tag list and the caveats always add up. An empty ``platforms`` under
    ``status: declared`` is the catalogue's own statement: the system block
    carries no ``<platform>`` tag (ES-DE warns about exactly that state).
    """

    system: str
    status: str
    tags_source: str
    platforms: tuple[str, ...] = ()
    identities: tuple[PlatformIdentities, ...] = ()
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


# The launchability verdicts (issue #36) — four claims that never collapse.
# "not-accepted" is a read of the machine: the frontend's accept-list for the
# system does not carry this file's extension, so ES-DE never scans it and
# nothing here will launch it. "needs-installation" is that same 'no' with its
# reason from world knowledge: the format is real content for the platform,
# and an installation step has to run before anything can launch (a PSN .pkg).
# "unknown" is a statement about the look, never about the file: the system is
# not one this catalogue declares, or the catalogue could not be read — a
# client that treated it as either of the other two would be told something
# nobody checked. Each constant spells its code.
VERDICT_LAUNCHABLE = "launchable"
VERDICT_NOT_ACCEPTED = "not-accepted"
# The accept-list is declared per system — the union over every entry — and
# the command per emulator, so the list can say yes while the entry that
# actually runs loads nothing (issue #66). This verdict is that split, stated
# only where the running entry's refusal is ESTABLISHED: a standalone whose
# recorded loader does not read the format, or a block-extract core handed an
# archive it does not claim. The remedies differ from not-accepted's, which
# is why the two never collapse: unpack the container, or select an entry
# that takes it — ``alternatives`` names the ones established to.
VERDICT_ENTRY_NOT_ACCEPTED = "entry-not-accepted"
VERDICT_NEEDS_INSTALLATION = "needs-installation"
VERDICT_UNKNOWN = "unknown"

LAUNCH_VERDICTS = (
    VERDICT_LAUNCHABLE,
    VERDICT_NOT_ACCEPTED,
    VERDICT_ENTRY_NOT_ACCEPTED,
    VERDICT_NEEDS_INSTALLATION,
    VERDICT_UNKNOWN,
)

# The per-entry half of a launchability answer, three statements apart. For a
# libretro entry the extension list is a CLAIM, not a gate: RetroArch checks
# nothing on a direct load and hands the file to the core, so a file outside
# the claims is attempted, never refused — stated, because a client deciding
# what to bake should know the claim is absent. Archives are the exception
# that runs through RetroArch's own hands: a container the core does not
# claim is opened and searched for a file matching the claims
# (task_content.c:1325-1358 @ a79435a) — what is inside is something atlas
# does not read. And an entry whose reading nobody established — a standalone
# without a card, a core that could not be probed — is exactly that, never
# "refuses".
CAVEAT_ENTRY_FORMAT_UNCLAIMED = "entry-format-unclaimed"
CAVEAT_ARCHIVE_CONTENTS_UNREAD = "archive-contents-unread"
CAVEAT_ENTRY_FORMAT_UNESTABLISHED = "entry-format-unestablished"

_ENTRY_ACCEPTS = "accepts"
_ENTRY_REFUSES = "refuses"
_ENTRY_UNESTABLISHED = "unestablished"


@dataclass(frozen=True, slots=True)
class LaunchabilityAnswer:
    """Whether one file launches as one system's content here — and why not, when not.

    ``extension`` is the token ES-DE would derive from the file
    (:func:`atlas.esde.esde_extension` — from the last dot, case preserved),
    stated on every verdict so a 'no' shows the exact string that missed.
    ``accepted`` is the system's declared list verbatim, empty where nothing
    was read. ``entry`` is the launch entry that would run — the frontend's
    own selection hierarchy applied, per-game override first — stated on the
    two verdicts where one exists: ``launchable``, and ``entry-not-accepted``,
    where the entry *is* the finding. ``alternatives`` travels with the
    latter alone: the labels of the declared entries established to take the
    file, which is one of the two remedies that verdict leaves open.
    """

    verdict: str
    extension: str
    accepted: tuple[str, ...] = ()
    entry: EmulatorEntry | None = None
    alternatives: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


# Flatpak's per-app overrides are GKeyFile INI, and only the environment they
# assign is read here — the [Environment] group's KEY=VALUE lines and the
# [Context] group's unset-environment list. Value semantics follow
# g_key_file_get_string, which is how flatpak reads them (flatpak 1.16.6,
# flatpak-context.c:1944): leading whitespace of a value is skipped and
# trailing whitespace kept (GLib 2.84.4 g_key_file_parse_key_value_pair), the
# escapes \s \n \t \r \\ are decoded, and a value whose escape does not decode
# is treated as an UNSET — flatpak hands the failed read on as NULL
# (flatpak-context.c:1944-1946, the GError is NULL) and a NULL value unsets
# the variable (flatpak-run.c:752-755). No $VAR is ever expanded: values are
# literal strings all the way into the sandbox environment. Group names match
# exactly, the way GKeyFile matches them — "[ Environment ]" is a different
# group. One deliberate leniency: lines GKeyFile would refuse outright are
# skipped here instead, because a file it cannot load at all stops
# `flatpak run` before any emulator exists to ask about (the load error
# propagates out of flatpak_load_override_file, flatpak-dir.c:2917-2940, and
# fails flatpak_dir_load_deployed, :3053-3083) — a machine whose app cannot
# launch is not a machine this parser has an answer for.
_GKEYFILE_ESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}


def _gkeyfile_string(value: str) -> str | None:
    """*value* as ``g_key_file_get_string`` answers it — ``None`` where that read fails.

    ``None`` is an escape GKeyFile refuses: a ``\\`` before anything outside
    the escape set, or one at the end of the line. The caller treats it as an
    unset, because that is what flatpak does with the failed read.
    """
    if "\\" not in value:
        return value
    decoded: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\":
            decoded.append(char)
            index += 1
            continue
        if index + 1 >= len(value):
            return None
        replacement = _GKEYFILE_ESCAPES.get(value[index + 1])
        if replacement is None:
            return None
        decoded.append(replacement)
        index += 2
    return "".join(decoded)


def _gkeyfile_list(value: str) -> tuple[str, ...]:
    """*value* as a GKeyFile string list: ``;``-separated, ``\\;`` escaping the separator.

    The trailing empty element is the writer's own trailing separator
    (``flatpak override`` always leaves one) and is dropped the way GKeyFile
    drops it. An element whose escapes do not decode is skipped — for flatpak
    that failure stops the app from launching at all (see the module comment
    above), which is not a state this parser models.
    """
    elements: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            if value[index + 1] == ";":
                current.append(";")
            else:
                current.append(char)
                current.append(value[index + 1])
            index += 2
            continue
        if char == ";":
            elements.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    elements.append("".join(current))
    if elements and elements[-1] == "":
        elements.pop()
    decoded = (_gkeyfile_string(element) for element in elements)
    return tuple(element for element in decoded if element is not None)


def _environment_overrides(text: str) -> dict[str, str | None]:
    """The environment assignments one overrides file makes: key -> value, ``None`` = unset.

    Both spellings of an assignment, in the order flatpak applies them within
    one file: the ``[Environment]`` group's values first, then the
    ``[Context]`` group's ``unset-environment`` list on top — the unset wins
    over a value in the same file, deliberately, so both can be written
    together for compatibility (flatpak-context.c:1935-1972, the comment at
    :1950-1953). Across files the caller composes: a later file's assignment,
    set or unset, overwrites an earlier file's per key.
    """
    environment: dict[str, str | None] = {}
    unset: tuple[str, ...] = ()
    group: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            continue
        if "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        key = key.strip()
        if group == "Environment":
            environment[key] = _gkeyfile_string(value.lstrip(" \t"))
        elif group == "Context" and key == "unset-environment":
            unset = _gkeyfile_list(value.lstrip(" \t"))
    for name in unset:
        environment[name] = None
    return environment


# Flatpak's overrides directories, one per installation. Both hold a file
# per app id and a file named "global" that applies to every app. Each of
# the four spellings was observed live under `strace` (flatpak 1.16.6,
# reference machine 2026-08-08): one `flatpak override --show` invocation
# opens exactly one file, and the four flag combinations — plain,
# `--user`, `<app id>`, `--user <app id>` — open these four in turn.
_FLATPAK_OVERRIDES_USER = os.path.join(_FLATPAK_USER_BASE, "overrides")
_FLATPAK_OVERRIDES_SYSTEM = os.path.join("/var", "lib", "flatpak", "overrides")
_FLATPAK_OVERRIDES_GLOBAL = "global"


def _flatpak_override_files(machine: Machine, home: str, app_id: str) -> tuple[str, ...]:
    """The overrides files that speak for *app_id*'s runs, least specific first.

    Flatpak's own composition, order and scope alike: within each
    installation the global file before the per-app one, the system
    installation before the user one, each later file overwriting the
    earlier per key (``flatpak_deploy_get_overrides``,
    flatpak-dir.c:1518-1567; the environment merge is a plain per-key
    hash insert, flatpak-context.c:1077-1079). The system files join only
    for an app whose *running* deploy the system installation carries
    (``flatpak_dir_load_deployed``, flatpak-dir.c:3053-3059 and :3071-3077
    @ 1.16.6, both gated on the installation not being the user one) — and
    which deploy runs is :func:`_running_deploy`'s single resolution, the
    same one the app's ``/app`` reads come out of. So a user deploy
    silences the system files even where a system deploy also exists, and a
    machine deploying the app nowhere runs nothing for any override to
    speak about — only the always-loaded user files are read then
    (flatpak-dir.c:3053-3083).
    """
    return _flatpak_override_files_for(_running_deploy(machine, home, app_id), home, app_id)


def _flatpak_override_files_for(deploy: _Deploy | None, home: str, app_id: str) -> tuple[str, ...]:
    """:func:`_flatpak_override_files` over an already-resolved deploy — one resolution per query."""
    directories = []
    if deploy is not None and deploy.system:
        directories.append(_FLATPAK_OVERRIDES_SYSTEM)
    directories.append(os.path.join(home, _FLATPAK_OVERRIDES_USER))
    return tuple(
        os.path.join(directory, name)
        for directory in directories
        for name in (_FLATPAK_OVERRIDES_GLOBAL, app_id)
    )


def _flatpak_environment(
    machine: Machine, home: str, app_id: str
) -> dict[str, tuple[str | None, str]]:
    """The environment the override files hand *app_id*'s runs: key -> (value, file).

    Composed the way flatpak composes it: every applicable file in
    :func:`_flatpak_override_files` order, later files overwriting earlier
    ones per key — a later set overwrites an earlier unset and vice versa,
    because the merge is one hash insert per key
    (flatpak-context.c:1077-1079). ``None`` is an unset. The file that
    had the last word travels with each value, so a statement can name
    what a user would edit.
    """
    files = _flatpak_override_files(machine, home, app_id)
    return _flatpak_environment_from(
        tuple((path, machine.read_text(path).text) for path in files)
    )


def _flatpak_environment_from(
    texts: tuple[tuple[str, str | None], ...],
) -> dict[str, tuple[str | None, str]]:
    """:func:`_flatpak_environment` over already-read texts — one read per query."""
    merged: dict[str, tuple[str | None, str]] = {}
    for path, text in texts:
        if text is None:
            continue
        for key, value in _environment_overrides(text).items():
            merged[key] = (value, path)
    return merged


def _flatpak_cfg_sandbox(machine: Machine, home: str, app_id: str) -> tuple[_Sandbox, tuple[str, ...]]:
    """The sandbox a Flatpak RetroArch cfg read resolves through, its ``~`` base from the override files.

    The one consequence a Flatpak override has on a cfg-reading query, and
    that query's one read of the override files. The config home itself
    cannot be moved: flatpak force-pins the ``XDG_*_HOME`` variables to the
    per-app directories AFTER applying every override and ``--env``
    (flatpak 1.16.6, flatpak-context.c:3158-3187 applied via
    flatpak-run.c:3574, against the context env applied at :3352, both with
    overwrite; flatpak-run(1) documents the pin; flatpak/flatpak#4529 — the
    request to lift it — closed as not planned). ``HOME`` is different: the
    host value passes into the sandbox and an override lands on top with
    nothing reapplied after (flatpak-run.c:3055, :3352), and the one thing
    it decides among these reads is what RetroArch substitutes for a ``~``
    in a cfg value (``getenv("HOME")``, file_path.c:1066-1101, :1457-1468
    @ a79435a).

    The override value is applied literally — flatpak expands no ``$`` —
    so an overridden HOME that is not a literal absolute path expands
    ``~`` into something that is not one either, and the ordinary
    machinery states what that shape earns (RetroArch's own directory
    test refuses it, or the sandbox translation cannot follow it). The
    source line is the statement that an override is in force at all.
    """
    environment = _flatpak_environment(machine, home, app_id)
    return _sandbox_from_environment(machine, home, app_id, environment)


def _sandbox_from_environment(
    machine: Machine, home: str, app_id: str, environment: dict[str, tuple[str | None, str]]
) -> tuple[_Sandbox, tuple[str, ...]]:
    """The ``~``-base decision of :func:`_flatpak_cfg_sandbox`, over a merged environment."""
    if "HOME" not in environment:
        return _Sandbox(machine, home, app_id, expansion_home=home), ()
    value, path = environment["HOME"]
    if not value:
        state = "HOME unset" if value is None else 'HOME = ""'
        line = (
            f"Flatpak overrides read live ({path}: {state}) — RetroArch leaves a ~ in cfg "
            "values unexpanded (file_path.c:1066-1101, :1457-1468)"
        )
    else:
        line = (
            f"Flatpak overrides read live ({path}: HOME) — a ~ in cfg values expands "
            f"against {value!r}"
        )
    return _Sandbox(machine, home, app_id, expansion_home=value or None), (line,)


# The filesystem half of the same override files (issue #103). Only what the
# revocation statement needs is modelled, and the model's edges are explicit:
# entries resolve to host paths for 'host', 'home', absolute paths, '~/…' and
# the three fixed XDG bases; every other token (the xdg-user-dirs aliases,
# host-os, host-etc) covers nothing here — those resolve through the user's
# user-dirs.dirs or name trees no save root lives under, and a token this
# model cannot place must neither fire a statement nor suppress one.
_FS_SPECIAL_TOKENS = frozenset(("home", "host", "host-etc", "host-os", "host-reset"))
# The '/' entries flatpak refuses to bind into the sandbox root under a 'host'
# grant (dont_mount_in_root, flatpak-context.c:2765-2786); /run/media is the
# carve-out exported explicitly beside them (:2884-2888).
_FS_HOST_UNMOUNTED = frozenset(
    ("app", "bin", "boot", "dev", "efi", "etc", "lib", "lib32", "lib64",
     "proc", "root", "run", "sbin", "sys", "tmp", "usr", "var")
)
_FS_XDG_BASES = {
    "xdg-data": _XDG_DATA_SUFFIX,
    "xdg-config": _XDG_CONFIG_DIRNAME,
    "xdg-cache": ".cache",
}


@dataclass(frozen=True, slots=True)
class _FsEntry:
    """One effective filesystem entry: hidden or granted, and who last said so."""

    hidden: bool
    source: str
    raw: str


def _context_filesystems(text: str) -> tuple[str, ...] | None:
    """The ``[Context]`` group's ``filesystems`` list of one file — ``None`` when absent.

    The same GKeyFile grammar the environment reads use: group names match
    exactly, the value is a ``;``-separated string list
    (:func:`_gkeyfile_list`).
    """
    group: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            continue
        if group != "Context" or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        if key.strip() == "filesystems":
            return _gkeyfile_list(value.lstrip(" \t"))
    return None


def _fs_entry_key(entry: str) -> tuple[str, bool]:
    """One filesystems entry → its table key and whether it is a revocation.

    Flatpak's own parse, reduced to what the visibility question needs: the
    ``!`` prefix (parse_negated, flatpak-context.c:1720), the ``:ro``/``:rw``/
    ``:create``/``:reset`` suffix cut (parse_filesystem_flags, :816-935 —
    a negated entry's mode is NONE whatever the suffix says), backslash
    escapes in the path part (:826-836), and ``!host:reset`` spelling
    ``host-reset`` (:906-909). Mode distinctions among the grants are
    deliberately not kept: ``:ro`` still *shows* the tree, and whether a
    read-only save root is its own finding is outside this issue's scope.
    """
    negated = entry.startswith("!")
    body = entry[1:] if negated else entry
    key_chars: list[str] = []
    i = 0
    while i < len(body) and body[i] != ":":
        if body[i] == "\\" and i + 1 < len(body):
            key_chars.append(body[i + 1])
            i += 2
            continue
        key_chars.append(body[i])
        i += 1
    key = "".join(key_chars)
    suffix = body[i + 1 :] if i < len(body) else ""
    if key == "host" and suffix == "reset":
        key = "host-reset"
    return key, negated


def _merge_filesystems(
    base: tuple[tuple[str, str | None], ...],
    overrides: tuple[tuple[str, str | None], ...],
) -> tuple[dict[str, _FsEntry], dict[str, _FsEntry]]:
    """The (metadata-only, effective) filesystem tables of one app's runs.

    Flatpak's merge, per layer: a layer carrying ``host-reset`` first clears
    everything merged so far (flatpak-context.c:1086-1090 — the override that
    says "start over"), then every entry is one hash insert per key
    (:1092-1096 via flatpak_context_take_filesystem). ``host-reset`` also
    implies ``!host`` (:1046-1051).
    """
    table: dict[str, _FsEntry] = {}

    def apply(path: str, text: str | None) -> None:
        if text is None:
            return
        entries = _context_filesystems(text)
        if entries is None:
            return
        parsed = [(entry, *_fs_entry_key(entry)) for entry in entries]
        if any(key == "host-reset" for _, key, _n in parsed):
            table.clear()
            table["host"] = _FsEntry(hidden=True, source=path, raw="!host-reset")
        for raw, key, negated in parsed:
            if key == "host-reset":
                continue
            table[key] = _FsEntry(hidden=negated, source=path, raw=raw)

    for path, text in base:
        apply(path, text)
    base_table = dict(table)
    for path, text in overrides:
        apply(path, text)
    return base_table, table


def _fs_resolve_entry(key: str, home: str) -> str | None:
    """One table key as the host path its export would cover — ``None`` outside the model.

    ``None`` for the special tokens (they compete through their own branches)
    and for every token the model does not place (the xdg-user-dirs aliases):
    those cover nothing and suppress nothing, which the module comment above
    the vocabulary states as the boundary.
    """
    if key in _FS_SPECIAL_TOKENS:
        return None
    if key.startswith("/"):
        return key
    if key.startswith("~/"):
        return os.path.join(home, key[2:])
    token, _, rest = key.partition("/")
    if token in _FS_XDG_BASES:
        base = os.path.join(home, _FS_XDG_BASES[token])
        return os.path.join(base, rest) if rest else base
    return None


def _host_export_prefix(path: str) -> str | None:
    """The export a ``host`` grant covers *path* through — ``None`` where it binds nothing.

    A ``host`` grant binds every root entry except flatpak's own reserved
    names, plus ``/run/media`` explicitly (flatpak-context.c:2856-2888).
    """
    if path == "/run/media" or path.startswith("/run/media/"):
        return "/run/media"
    first = path.split("/", 2)[1] if path.startswith("/") else ""
    if first and first not in _FS_HOST_UNMOUNTED:
        return f"/{first}"
    return None


class _FsCompetition:
    """The export that decides one path's visibility — flatpak's own tie rules.

    The most specific covering export wins (path_is_mapped walks the sorted
    keys and lets the longest covering prefix overwrite the verdict,
    flatpak-exports.c:340-378). Two entries resolving to the SAME path
    collapse into one export with the higher mode winning — a grant beats
    the tmpfs hide (do_export_path, :760-798; FAKE_MODE_TMPFS is MODE_NONE,
    :102). Live consequence: ``!/run/media`` beside a ``host`` grant hides
    nothing, because host exports ``/run/media`` itself.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._best = -1
        self.decider: _FsEntry | None = None

    def offer(self, prefix: str, entry: _FsEntry) -> None:
        if self._path != prefix and not self._path.startswith(prefix.rstrip("/") + "/"):
            return
        grant_wins_tie = (
            len(prefix) == self._best
            and self.decider is not None
            and self.decider.hidden
            and not entry.hidden
        )
        if len(prefix) > self._best or grant_wins_tie:
            self._best = len(prefix)
            self.decider = entry


def _fs_visible(table: dict[str, _FsEntry], home: str, path: str) -> tuple[bool, _FsEntry | None]:
    """Whether *path* is visible under *table* — and the entry that decided.

    The application semantics, ported (:class:`_FsCompetition` holds the tie
    rules): a revoked path entry is exported as a tmpfs and therefore hides
    even under a broader grant (flatpak_exports_add_path_expose_or_hide,
    flatpak-exports.c:1096-1108), while a revoked special token simply
    exports nothing (flatpak_context_export skips a NONE mode,
    flatpak-context.c:2844-2919).
    """
    competition = _FsCompetition(path)
    host = table.get("host")
    if host is not None and not host.hidden:
        prefix = _host_export_prefix(path)
        if prefix is not None:
            competition.offer(prefix, host)
    # A 'home' grant exports $HOME (flatpak-context.c:2903-2919). A negated
    # one exports nothing — special tokens are never mounted over as tmpfs
    # (context_export skips them, :2930-2932) — so under a live 'host' grant
    # $HOME stays reachable through the /home root bind whatever '!home'
    # says: 'home' is not among the reserved root names.
    home_entry = table.get("home")
    if home_entry is not None and not home_entry.hidden:
        competition.offer(home, home_entry)
    for key, entry in table.items():
        resolved = _fs_resolve_entry(key, home)
        if resolved is not None:
            competition.offer(resolved.rstrip("/") or "/", entry)
    decider = competition.decider
    if decider is None:
        return False, None
    return not decider.hidden, decider


@dataclass(frozen=True, slots=True)
class _FlatpakQueryContext:
    """One query's read of the flatpak seams: the cfg sandbox, and the revocation check.

    ``revocation`` answers for one resolved host path: ``None`` when the app
    can see it (or when nothing establishes it cannot), the caveat when an
    override file revokes access the app's own metadata grants. Differential
    on purpose: a path the metadata never granted is not *revoked* — that is
    a different statement nobody asked for here — so the check fires exactly
    when visibility flips from the metadata-only table to the effective one.
    """

    sandbox: _Sandbox
    sources: tuple[str, ...]
    base_table: dict[str, _FsEntry]
    effective_table: dict[str, _FsEntry]
    home: str

    def revocation(self, path: str) -> Caveat | None:
        if not path.startswith("/"):
            return None
        visible_before, _ = _fs_visible(self.base_table, self.home, path)
        visible_after, decider = _fs_visible(self.effective_table, self.home, path)
        if not visible_before or visible_after:
            return None
        if decider is not None and decider.hidden and decider.source:
            taken = f"{decider.source} revokes it ({decider.raw})"
            data = {"path": path, "entry": decider.raw, "options_file": decider.source}
        else:
            # The grant fell away wholesale (a negated special token): name
            # the entry that dropped it, which is the last hidden special
            # token an override stated.
            dropped = next(
                (
                    entry
                    for key, entry in self.effective_table.items()
                    if key in _FS_SPECIAL_TOKENS and entry.hidden and entry.source
                ),
                None,
            )
            if dropped is None:
                return None
            taken = f"{dropped.source} drops the grant ({dropped.raw})"
            data = {"path": path, "entry": dropped.raw, "options_file": dropped.source}
        return Caveat(
            CAVEAT_SAVE_ROOT_REVOKED,
            f"the app cannot touch {path}: its own metadata grants the filesystem access and "
            f"{taken} — a revoked path is mounted over as an empty tmpfs "
            "(flatpak-exports.c:1096-1108 @ 1.16.6), so what the emulator writes there never "
            "lands in this directory on the host",
            data,
        )


def _flatpak_query_context(machine: Machine, home: str, app_id: str) -> _FlatpakQueryContext:
    """One read of every flatpak seam a save query consults (issue #101 + #103).

    The deploy is resolved once and serves both consumers: the override-file
    scope, and the app metadata whose ``[Context] filesystems`` is the grant
    base the revocation check measures against. Every file is read exactly
    once — the environment merge and the filesystem merge run over the same
    texts.
    """
    deploy = _running_deploy(machine, home, app_id)
    files = _flatpak_override_files_for(deploy, home, app_id)
    texts = tuple((path, machine.read_text(path).text) for path in files)
    sandbox, sources = _sandbox_from_environment(
        machine, home, app_id, _flatpak_environment_from(texts)
    )
    metadata: tuple[tuple[str, str | None], ...] = ()
    if deploy is not None:
        metadata_path = os.path.join(os.path.dirname(deploy.files), "metadata")
        metadata = ((metadata_path, machine.read_text(metadata_path).text),)
    base_table, effective_table = _merge_filesystems(metadata, texts)
    return _FlatpakQueryContext(
        sandbox=sandbox,
        sources=sources,
        base_table=base_table,
        effective_table=effective_table,
        home=home,
    )


# The ways a READ catalogue still yields no ROM directory. The first two are
# facts about the machine — the catalogue declares nothing, or the frontend's
# own setting is not a path anything can be resolved against — and a client acts
# on them by fixing the machine. The third is a statement about atlas: the
# file that decides the directory could not be read. Three facts, three codes:
# a client branches on the code, and prose is the one thing it cannot branch on.
CAVEAT_ROM_PATH_UNDECLARED = "rom-path-undeclared"
CAVEAT_ROM_PATH_UNRESOLVED = "rom-path-unresolved"
CAVEAT_FRONTEND_SETTINGS_UNREADABLE = "frontend-settings-unreadable"

# ES-DE's on-disk relocation switch: a portable.txt beside an EmuDeck-managed
# AppImage may move the tree every ~/ES-DE read comes from, and the EmuDeck
# handle's rider states that suspicion under this code. A Flatpak override
# cannot earn it: flatpak force-pins the XDG_*_HOME variables to the per-app
# directories AFTER applying every override and --env (flatpak 1.16.6,
# flatpak-context.c:3158-3187 applied via flatpak-run.c:3574, against the
# override env applied at :3352, both with overwrite; flatpak-run(1) documents
# the pin, and flatpak/flatpak#4529 — the request to make these overridable —
# was closed as not planned), so the config home a RetroDECK answer reads from
# is exactly the one in force, override files or no.
CAVEAT_CONFIG_HOME_RELOCATED = "config-home-relocated"

# Why the settings could not be read, where the read itself succeeded and the
# parse did not. Alongside the seam's own read statuses in the caveat's data,
# because to a client they answer the same question — and it is not one of
# them: the bytes arrived.
_SETTINGS_UNPARSEABLE = "unparseable"


# The catalogue file's own name, spelled once: ES-DE uses it for the bundled
# layer, the custom_systems overlay and the resource-override shadow alike,
# and the three probes below must never drift apart on it.
_ES_SYSTEMS_XML = "es_systems.xml"


def _catalogue_unread_caveat(system: str | None = None) -> tuple[Caveat, ...]:
    """The caveat for a catalogue that could not be read — the same fact as the firmware route's.

    One fact, one code: ``firmware_for_system`` already states this exact
    thing when its catalogue comes back unread, and an answer that is empty
    because nobody could look is the same answer whichever door it left by.
    Module-level because it is the same fact on every catalogued arrangement —
    RetroDECK's bundled layer and EmuDeck's on-disk one degrade through the
    one builder, so the two can never spell the claim apart.
    """
    return (
        Caveat(
            CAVEAT_EMULATOR_CATALOGUE_UNREADABLE,
            "the frontend's emulator catalogue could not be read, so which emulators this "
            "installation knows is unknown — this answer is empty because atlas could not look, "
            "not because nothing is there",
            {"system": system} if system is not None else {},
        ),
    )


def _catalogue_exclusive_caveat(path: str, system: str | None = None) -> tuple[Caveat, ...]:
    """The custom catalogue declared itself exclusive — a statement, not a degradation.

    ES-DE honors a document-level ``<loadExclusive/>`` only in the custom
    ``es_systems.xml``: the bundled file is then never opened
    (``SystemData::loadConfig``, ``es-app/src/SystemData.cpp:858-895``, ES-DE
    v3.4.1; documented in INSTALL.md v3.4.1:1466). The enumeration on such a
    machine is the custom layer alone, and it is **complete** — nothing
    bundled is in force, so nothing here hedges the way ``sealed`` does.
    Module-level for the same reason as the unread caveat: both catalogued
    arrangements state this one fact through one builder.
    """
    return (
        Caveat(
            CAVEAT_EMULATOR_CATALOGUE_EXCLUSIVE,
            f"the custom es_systems.xml at {path} carries a document-level <loadExclusive/>, so "
            "the frontend loads it alone and the bundled catalogue is not loaded — this "
            "enumeration is the custom layer only, and it is the complete catalogue in force",
            {"system": system} if system is not None else {},
        ),
    )


# What an exclusive answer rests on: the one layer that was read. Module-level
# like the caveat builder — the two handles must not spell the reading apart.
_CATALOGUE_SOURCE_EXCLUSIVE = (
    "ES-DE catalogue read live (es_systems.xml, custom_systems overlay only — loadExclusive)"
)


def _rom_path_undeclared_caveat(
    system: str, declaration: SystemDeclaration | None
) -> tuple[Caveat, ...]:
    """A read catalogue that names no directory for this system.

    The two ways it happens — no such system, or a system declared without
    a ``<path>`` — are one code and one thing to do about it, so they
    differ in the message and not in the data. What a client acts on is
    that this catalogue states no directory.
    """
    subject = (
        f"declares no system {system!r}"
        if declaration is None
        else f"declares {system!r} without a <path>"
    )
    return (
        Caveat(
            CAVEAT_ROM_PATH_UNDECLARED,
            f"the frontend's catalogue was read and {subject}, so where its ROMs live is not "
            "something this machine states — the answer is empty because the declaration is, "
            "not because atlas could not look",
            {"system": system},
        ),
    )


def _rom_path_unresolved_caveat(system: str, declared: str, rom_directory: str) -> tuple[Caveat, ...]:
    """A configured ROM directory that is not a path a ``%ROMPATH%`` can be resolved against.

    One fact and one code, now that the other reasons have theirs: the
    frontend's setting holds something that is not absolute even after the
    frontend's own ``~`` expansion (``atlas.esde.expand_home_path``) — a
    relative path, whose base is the ES-DE process's working directory, or a
    ``%ESPATH%`` spelling, whose base is the frontend's binary directory
    (``FileData.cpp:300-302``, v3.4.1) — and neither base is something atlas
    has established. A fact about the machine, and the thing to do about it
    is to fix the setting.

    The declaration travels in the data: it is the fact atlas *did*
    establish, and a caller who knows their own setup can finish the
    substitution atlas refused to guess at. ``configured`` is the setting's
    own text, unexpanded — the value whose remedy is an edit.
    """
    return (
        Caveat(
            CAVEAT_ROM_PATH_UNRESOLVED,
            f"the catalogue declares {system!r} at {declared!r}, and ES-DE's ROMDirectory is "
            f"{rom_directory!r}, which is not an absolute path — atlas states no directory rather "
            "than guessing one, because a ROM directory guessed wrong is a real directory the "
            "caller would go looking in",
            {"system": system, "declared": declared, "configured": rom_directory},
        ),
    )


def _settings_unreadable_caveat(system: str, path: str, status: str) -> tuple[Caveat, ...]:
    """The frontend's settings are there and atlas could not read them.

    A statement about atlas, not about the machine, and the reason it is
    not the unset case: the frontend reads this file, so the ROM directory
    it names is the one in force — and falling back on the default here
    would state a directory belonging to a configuration nobody established.
    """
    return (
        Caveat(
            CAVEAT_FRONTEND_SETTINGS_UNREADABLE,
            f"the frontend's settings at {path} exist and could not be read ({status}), so what "
            f"they say about {system!r}'s ROM directory is unknown — atlas states none rather "
            "than the default, which only applies to a file that sets nothing",
            {"system": system, "path": path, "status": status},
        ),
    )


def _per_game_override_caveat(override_label: str, spec: EmulatorSpec) -> Caveat:
    """This game's gamelist entry selects a different emulator than *spec*.

    One statement on both arrangements' entry routes: which emulator ES-DE
    would actually launch decides the placement, and an entry that would not
    launch this game says so instead of answering as if it would.
    """
    return Caveat(
        CAVEAT_PER_GAME_OVERRIDE,
        f"this game carries a per-game altemulator override selecting "
        f"{override_label!r} — ES-DE would launch that emulator, not "
        f"{spec.label!r}; ask emulators_for with content_path",
        {"label": override_label},
    )


def _per_game_alternative_emulator_caveat(per_game: Mapping[str, str]) -> Caveat:
    """Some games of this system select a different emulator — every entry says so.

    The statement of a system-level ask (#311): *per_game* maps each gamelist
    entry to its ``<altemulator>`` label, so the count and the per-label tally
    in ``emulators`` are read from the same elements — no second read. The
    tally is sorted by label, because an aggregate has no upstream order to
    preserve.
    """
    tally: dict[str, int] = {}
    for label in per_game.values():
        tally[label] = tally.get(label, 0) + 1
    emulators = {label: str(n) for label, n in sorted(tally.items())}
    return Caveat(
        CAVEAT_PER_GAME_ALTERNATIVE_EMULATOR,
        f"{len(per_game)} game(s) of this system carry per-game altemulator "
        f"overrides, selecting {', '.join(emulators)} — this system-level order may be "
        "wrong for exactly those games; ask emulators_for with content_path",
        {"count": str(len(per_game)), "emulators": emulators},
    )


def _entries_from(
    host: "_CatalogueHost",
    specs: tuple[EmulatorSpec, ...],
    selections: GamelistSelections,
    *,
    system_roms_dir: str | None,
    content_path: str | None,
) -> tuple[EmulatorEntry, ...]:
    """Apply ES-DE's selection hierarchy to one already-read catalogue snapshot.

    Module-level and host-parameterized: the hierarchy is ES-DE's, not one
    arrangement's, so the catalogue answer and the firmware route of every
    ES-DE-driven handle assemble their entries here instead of re-reading the
    sources — or worse, growing a second copy of the promotion rule.

    ``system_roms_dir`` is ``None`` where there is no anchor to match
    against — either nothing named content, or the directory could not be
    resolved. Per-game matching is skipped either way; the caller that
    asked for it is the one holding the caveat that says why.
    """
    chosen_label: str | None = None
    chosen_source: str | None = None
    if content_path is not None and system_roms_dir is not None:
        per_game = _match_per_game(selections, content_path, system_roms_dir=system_roms_dir)
        if per_game is not None:
            chosen_label = per_game
            chosen_source = f'gamelist.xml: altemulator = "{per_game}" (per-game)'
    if chosen_label is None and selections.system_label is not None:
        chosen_label = selections.system_label
        chosen_source = f'gamelist.xml: alternativeEmulator = "{selections.system_label}"'
    if specs and chosen_label is not None:
        for index, spec in enumerate(specs):
            if spec.label == chosen_label:
                promoted = _dc_replace(spec, selection=chosen_source)
                specs = (promoted, *specs[:index], *specs[index + 1 :])
                break
    entry_caveats: tuple[Caveat, ...] = ()
    if content_path is None and selections.per_game:
        entry_caveats = (_per_game_alternative_emulator_caveat(selections.per_game),)
    return tuple(EmulatorEntry(host, spec, entry_caveats) for spec in specs)


def _firmware_catalogue_entries(
    host: "_CatalogueHost",
    by_system: Mapping[str, SystemDeclaration],
    system: str,
    selections: GamelistSelections,
) -> tuple[CatalogueEntry, ...]:
    """The catalogue's emulator list for *system*, shaped for the firmware seam.

    Module-level for the same reason :func:`_entries_from` is: both ES-DE-driven
    handles hand their firmware route the same projection of the same assembly,
    and a second copy is how the two would drift apart. No content is named on
    the firmware route, so no per-game entry can match and the anchor is never
    consulted — the enumeration and its gamelist promotion are all that cross
    the seam.
    """
    entries = _entries_from(
        host,
        _declared_entries(by_system, system),
        selections,
        system_roms_dir=None,
        content_path=None,
    )
    shaped: list[CatalogueEntry] = []
    for entry in entries:
        token = None
        homes = None
        if entry.kind != KIND_LIBRETRO:
            token = host.standalone_firmware_token(entry.command)
            homes = host.standalone_firmware_homes(entry.command)
        shaped.append(
            CatalogueEntry(
                label=entry.label,
                kind=entry.kind,
                core_so=entry.core_so,
                standalone_token=token,
                standalone_data_home=homes.data if homes is not None else None,
                standalone_config_home=homes.config if homes is not None else None,
                standalone_flatpak=homes.flatpak if homes is not None else None,
            )
        )
    return tuple(shaped)


@dataclass(frozen=True, slots=True)
class _RomRoot:
    """What ES-DE substitutes for ``%ROMPATH%``, or which way it could not be established.

    ``directory`` is set exactly when the three refusal fields are not: an
    absolute root the frontend would really use, whether that is the configured
    value or ES-DE's own default for a file that sets nothing.

    The refusals stay apart rather than collapsing into one "no root", because
    each becomes a different caveat code — and they carry only what those
    messages need, not the messages themselves: two of the three name the
    system's declared ``<path>``, which the root does not know and has no
    business knowing. ``relocated`` is EmuDeck's alone — the on-disk
    ``portable.txt`` switch; RetroDECK's root has no relocated state, because
    flatpak pins the home its resolutions derive from (see
    :meth:`RetroDeck._rom_root`).
    """

    directory: str | None = None
    unreadable: str | None = None
    relocated: tuple[str, str] | None = None
    not_absolute: str | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _RomDirectory:
    """One system's ROM directory as ES-DE resolves it, and what was read to say so.

    ``sources`` is not the same question as whether a directory came out:
    settings that were read and set nothing still resolve — through the
    frontend's own default — while settings nobody could read resolve nothing
    and are no source at all.
    """

    directory: str | None = None
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


# The resolution a query skips because it has nothing to anchor: no content
# path was named, so no per-game entry can match and the directory is never
# consulted. Distinct from a resolution that refused — that one carries the
# caveat saying so.
_NO_ANCHOR_NEEDED = _RomDirectory()


@dataclass(frozen=True, slots=True)
class RomPlacement:
    """Where one system's ROMs live, and which files the frontend launches.

    Both come from the catalogue's own ``<system>`` declaration, so both are
    facts about this machine rather than a table: ``dir`` is the ``<path>``
    with ES-DE's ``%ROMPATH%`` substituted from the setting ES-DE substitutes
    it from, and ``extensions`` is the ``<extension>`` list as declared.

    ``dir`` is ``None`` wherever atlas could not resolve one, and never a
    partial path: a caller acts on this by looking in a directory, so a
    half-resolved string would send it somewhere real and wrong. Which kind of
    ``None`` it is — an arrangement with no catalogue, one whose catalogue atlas
    has not located, one whose catalogue could not be read, one whose readable
    layers declare nothing while the rest is sealed away, a system the
    catalogue declares no path for, a setting that is not a path, settings
    nobody could read, or a relocated config home — is a caveat, exactly as an
    empty :class:`CatalogueAnswer` is.

    ``extensions`` is the frontend's declaration, not a filter atlas applies:
    the tokens are verbatim, both cases are listed where the file lists both,
    and what to do with them is the caller's business.

    ``physical_dir`` is the fully link-resolved backing directory when ``dir``
    reaches its files through symlinks, and ``None`` otherwise — the same pair
    and the same convention :class:`~atlas.placement.SavefilePlacement` answers
    with, because it is the same question: a distribution that wires a tree
    into place with symlinks leaves the frontend-side path and the physical
    path as two truthful answers, and a client copying files may need either.
    A traversal that ends nowhere resolves to ``None`` and says so as a
    ``dead-symlink`` or ``symlink-loop`` caveat.
    """

    dir: str | None = None
    physical_dir: str | None = None
    extensions: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()


def _declared_entries(
    by_system: Mapping[str, SystemDeclaration], system: str
) -> tuple[EmulatorSpec, ...]:
    """The launch entries a read catalogue declares for *system* — none if it declares no such system."""
    declaration = by_system.get(system)
    return () if declaration is None else declaration.entries


class EmulatorEntry:
    """One catalogue entry — an emulator that can launch one system, as configured.

    Wraps an :class:`~atlas.esde.EmulatorSpec` and the installation it belongs
    to, so the entry can answer placement questions with its core always known —
    the ``no-core`` caveat class does not exist on this path.
    """

    def __init__(
        self, installation: "_CatalogueHost", spec: EmulatorSpec, caveats: tuple[Caveat, ...] = ()
    ) -> None:
        self._installation = installation
        self._spec = spec
        self._caveats = caveats

    @property
    def system(self) -> str:
        return self._spec.system

    @property
    def label(self) -> str:
        return self._spec.label

    @property
    def kind(self) -> str:
        return self._spec.kind

    @property
    def core_so(self) -> str | None:
        return self._spec.core_so

    @property
    def command(self) -> str:
        return self._spec.command

    @property
    def provenance(self) -> str:
        """Which catalogue layer declared this entry — prose, for debugging."""
        return self._spec.provenance

    @property
    def selection(self) -> str | None:
        """Provenance of a user promotion, or ``None`` for declared order."""
        return self._spec.selection

    @property
    def caveats(self) -> tuple[Caveat, ...]:
        """Stated catalogue-level degradations (e.g. unchecked per-game overrides)."""
        return self._caveats

    def savefile_location(self, *, content_path: str | None = None) -> SavefilePlacement | Unresolved:
        """Where this emulator keeps the save — core filled in from the catalogue.

        Catalogue-level degradations stay attached to the derived answer
        (REVIEW M9). A standalone entry answers where a packaged standalone
        save card covers the emulator the command names, and refuses with a
        domain outcome (:class:`~atlas.placement.Unresolved`) where none does —
        never a guess and never an exception (REVIEW M8). Which of the two it
        is stays the installation's decision, exactly as the texture question
        decides it: the catalogue names an emulator, and whether atlas has its
        wiring is a question about packaged knowledge.
        """
        return self._installation.entry_savefile_location(
            self._spec, self._caveats, content_path=content_path
        )

    def savestate_location(
        self, *, content_path: str | None = None
    ) -> SavestatePlacement | SavestateAbsence | Unresolved:
        """Where this emulator keeps the savestates — core filled in from the catalogue.

        The savefile route's twin, and since #225 it answers on the same
        entries too: a standalone entry answers where a packaged standalone
        savestate card covers the emulator the command names, and refuses
        with a domain outcome where none does — never a guess and never an
        exception. Which of the two it is stays the installation's decision,
        exactly as on the savefile route: the catalogue names an emulator,
        and whether atlas has its wiring is a question about packaged
        knowledge. Since #284 there is a third shape: a card can state, with
        its citation, that the emulator has no savestates at all — an answer
        (:class:`~atlas.placement.SavestateAbsence`), not a refusal.
        """
        return self._installation.entry_savestate_location(
            self._spec, self._caveats, content_path=content_path
        )

    def texture_pack_location(self, *, content_path: str | None = None) -> TexturePlacement | Unresolved:
        """Where this emulator reads texture packs — the emulator taken from the catalogue.

        The one question of the three that does **not** short-circuit on a
        standalone entry, and the asymmetry is deliberate. A save routes through
        a config atlas would have to model; a texture pack mostly does not — a
        standalone emulator opens its own default directory below an XDG base a
        flatpak pins, which is a path join and a symlink walk. So the same entry
        can refuse ``savefile_location`` and answer this, and the answer says
        what it could not read (``emulator-config-unread``) rather than
        pretending the switch is off.

        Which of the two it is stays the installation's decision, not a kind
        check here: the catalogue names an emulator, and whether atlas has that
        emulator's wiring is a question about packaged knowledge.
        """
        return self._installation.entry_texture_pack_location(
            self._spec, self._caveats, content_path=content_path
        )

    def mod_location(self, *, content_path: str | None = None) -> ModPlacement | Unresolved:
        """Where this emulator reads mods — the emulator taken from the catalogue.

        Answers for both kinds of entry, exactly as the texture question does
        and for the same reason: a standalone emulator's mod directory is
        mostly its own default below an XDG base a flatpak pins, which is a
        path join and a symlink walk rather than a configuration to model. So
        the same entry can refuse ``savefile_location`` and answer this.
        """
        return self._installation.entry_mod_location(
            self._spec, self._caveats, content_path=content_path
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"EmulatorEntry(system={self._spec.system!r}, label={self._spec.label!r}, "
            f"kind={self._spec.kind!r}, core_so={self._spec.core_so!r})"
        )


# One caveat-replacement helper per answer type, monomorphic on purpose: a
# generic one would hand Sonar's type engine the very TypeVar it cannot carry
# (it types dataclasses.replace as bare DataclassInstance), and the concrete
# signatures are what keep the declared type standing at every call site. Each
# helper holds the one cast: Sonar's engine does not carry replace's generic;
# basedpyright resolves it — the cast is for the weaker engine, made true by
# the signature's contract with its callers.


def _systems_with_caveats(answer: SystemsAnswer, caveats: tuple[Caveat, ...]) -> SystemsAnswer:
    """*answer* with *caveats* as its caveat list — ``dataclasses.replace`` behind a concrete signature."""
    return cast(SystemsAnswer, _dc_replace(answer, caveats=caveats))


def _rom_placement_with_caveats(answer: RomPlacement, caveats: tuple[Caveat, ...]) -> RomPlacement:
    """*answer* with *caveats* as its caveat list — ``dataclasses.replace`` behind a concrete signature."""
    return cast(RomPlacement, _dc_replace(answer, caveats=caveats))


def _catalogue_with_caveats(answer: CatalogueAnswer, caveats: tuple[Caveat, ...]) -> CatalogueAnswer:
    """*answer* with *caveats* as its caveat list — ``dataclasses.replace`` behind a concrete signature."""
    return cast(CatalogueAnswer, _dc_replace(answer, caveats=caveats))


def _launchable_with_caveats(
    answer: LaunchabilityAnswer, caveats: tuple[Caveat, ...]
) -> LaunchabilityAnswer:
    """*answer* with *caveats* as its caveat list — ``dataclasses.replace`` behind a concrete signature."""
    return cast(LaunchabilityAnswer, _dc_replace(answer, caveats=caveats))


class _EntryCoreReader:
    """The installed core behind a libretro entry — read lazily, each source once.

    The launchability question pays the RetroArch cfg read (and the override
    files composing its sandbox) only when an entry judgment actually needs a
    core: a standalone-only judgment never opens them. One cfg snapshot and
    one probe per ``.so`` serve every entry judged inside one answer, so the
    running entry and the alternatives can never read two revisions of the
    same file.
    """

    def __init__(
        self,
        machine: Machine,
        cfg_path: str,
        cfg_sandbox: "Callable[[], tuple[_Sandbox, tuple[str, ...]]]",
    ) -> None:
        self._machine = machine
        self._cfg_path = cfg_path
        self._cfg_sandbox = cfg_sandbox
        self._context: tuple[_Sandbox, str | None] | None = None
        self._infos: dict[str, CoreInfo | None] = {}
        self.sources: tuple[str, ...] = ()

    def __call__(self, entry: EmulatorEntry) -> CoreInfo | None:
        if entry.core_so is None:
            return None
        if entry.core_so in self._infos:
            return self._infos[entry.core_so]
        if self._context is None:
            sandbox, self.sources = self._cfg_sandbox()
            self._context = (sandbox, self._machine.read_text(self._cfg_path).text)
        sandbox, global_text = self._context
        lookup = _core_path_from(sandbox, global_text, entry.core_so)
        info = self._machine.query_core(lookup.so_path) if lookup.so_path is not None else None
        self._infos[entry.core_so] = info
        return info


def _loader_archive_token(extension: str) -> bool:
    """Whether RetroArch's loader treats this extension as a compressed container.

    ``path_is_compressed_file`` replicated exactly (file_path.c:294-320 @
    a79435a): ``zip``, ``zst`` and ``apk`` case-folded per character — and
    ``7z`` folding only its first position, so a ``.7Z`` is *not* compressed
    to this loader. The quirk is upstream's, and smoothing it over would
    state a behaviour the shipped binary does not have.
    """
    rest = extension[1:] if extension.startswith(".") else extension
    if len(rest) == 2 and rest[0] == "7":
        return rest[1] == "z"
    return rest.lower() in ("zip", "zst", "apk")


def _core_claims(info: CoreInfo | None) -> frozenset[str] | None:
    """The core's ``valid_extensions``, split and folded the way RetroArch matches them.

    Lowercase because every comparison RetroArch makes against this list is
    case-insensitive (``string_list_find_elem``'s ``|32`` folding and the
    archive filter's ``tolower``, string_list.c:342-390 @ a79435a). ``None``
    is a core nobody read, never an empty claim.
    """
    if info is None or info.valid_extensions is None:
        return None
    return frozenset(token.lower() for token in info.valid_extensions.split("|") if token)


def _entry_reading(
    entry: EmulatorEntry,
    *,
    extension: str,
    core_info_for: "Callable[[EmulatorEntry], CoreInfo | None]",
) -> tuple[str, tuple[str, ...], tuple[Caveat, ...]]:
    """One entry's stance on one extension: accepts, refuses, or unestablished.

    The two kinds of entry split along the boundary rule (issue #66). A
    libretro entry's claims are read live off the installed core, and they
    are claims: RetroArch checks nothing on a direct load, so a file outside
    them is attempted with a statement, never refused — except an archive,
    which runs through RetroArch's own hands (extracted and searched by the
    claims, or handed raw to a ``block_extract`` core that never claimed
    it, which is the one libretro refusal this can establish). A standalone
    entry opens the file itself: its recorded loader decides, and an
    emulator without a card is an entry nobody read.
    """
    if entry.kind == KIND_LIBRETRO:
        return _libretro_entry_reading(entry, extension=extension, info=core_info_for(entry))
    return _standalone_entry_reading(entry, extension=extension)


def _libretro_entry_reading(
    entry: EmulatorEntry, *, extension: str, info: CoreInfo | None
) -> tuple[str, tuple[str, ...], tuple[Caveat, ...]]:
    """The libretro half of :func:`_entry_reading` — claims, and RetroArch's archive hands."""
    bare = extension[1:].lower() if extension.startswith(".") and len(extension) > 1 else None
    claims = _core_claims(info)
    if claims is None:
        return (
            _ENTRY_UNESTABLISHED,
            (),
            (
                Caveat(
                    CAVEAT_ENTRY_FORMAT_UNESTABLISHED,
                    f"whether {entry.label!r} reads {extension!r} is not established — the "
                    "installed core could not be read, and a claim nobody read is not a "
                    "refusal",
                    {"entry": entry.label, "extension": extension},
                ),
            ),
        )
    if bare is not None and bare in claims:
        return (
            _ENTRY_ACCEPTS,
            (
                f"entry {entry.label!r}: the installed core claims {extension!r} "
                "(valid_extensions, read live off the binary; RetroArch's comparisons "
                "against that list fold case, string_list.c:342-390 @ a79435a)",
            ),
            (),
        )
    if _loader_archive_token(extension):
        if info is not None and info.block_extract:
            return (
                _ENTRY_REFUSES,
                (
                    f"entry {entry.label!r}: the core does not claim {extension!r} and sets "
                    "block_extract, so RetroArch hands it the archive raw instead of picking "
                    "a matching file out of it (task_content.c:742, :1735 @ a79435a) — a "
                    "container the core never claimed to read",
                ),
                (),
            )
        return (
            _ENTRY_ACCEPTS,
            (),
            (
                Caveat(
                    CAVEAT_ARCHIVE_CONTENTS_UNREAD,
                    f"{extension!r} is a container RetroArch opens for {entry.label!r}: the "
                    "first file inside matching the core's claims is what loads "
                    "(task_content.c:1325-1358 @ a79435a) — whether one is in there is "
                    "inside the archive, which atlas does not read",
                    {"entry": entry.label, "extension": extension},
                ),
            ),
        )
    return (
        _ENTRY_ACCEPTS,
        (),
        (
            Caveat(
                CAVEAT_ENTRY_FORMAT_UNCLAIMED,
                f"the installed core behind {entry.label!r} does not claim {extension!r} — "
                "RetroArch checks nothing on a direct load and hands the file over, so the "
                "core will attempt it and may fail; the claim's absence is stated, not a "
                "refusal",
                {"entry": entry.label, "extension": extension},
            ),
        ),
    )


def _standalone_entry_reading(
    entry: EmulatorEntry, *, extension: str
) -> tuple[str, tuple[str, ...], tuple[Caveat, ...]]:
    """The standalone half of :func:`_entry_reading` — the recorded loader decides."""
    card = lookup_standalone_launch(emulator_token(entry.command))
    if card is None:
        return (
            _ENTRY_UNESTABLISHED,
            (),
            (
                Caveat(
                    CAVEAT_ENTRY_FORMAT_UNESTABLISHED,
                    f"what {entry.label!r} reads is not established — no card covers this "
                    "standalone emulator's loader, which says nothing about whether it takes "
                    f"{extension!r}",
                    {"entry": entry.label, "extension": extension},
                ),
            ),
        )
    if card.takes(extension):
        return (
            _ENTRY_ACCEPTS,
            (f"entry {entry.label!r}: its own loader reads {extension!r} — {card.source}",),
            (),
        )
    archive_note = (
        " (an archive container, and this loader opens none — no RetroArch stands in front of "
        "a standalone to pick a file out of it)"
        if _loader_archive_token(extension) and not card.archives
        else ""
    )
    return (
        _ENTRY_REFUSES,
        (
            f"entry {entry.label!r}: its own loader does not read {extension!r}{archive_note} — "
            f"{card.source}",
        ),
        (),
    )


def _launchability_verdict(
    *,
    system: str,
    extension: str,
    declaration: SystemDeclaration | None,
    entries: tuple[EmulatorEntry, ...],
    complete: bool,
    core_info_for: "Callable[[EmulatorEntry], CoreInfo | None]",
) -> tuple[str, EmulatorEntry | None, tuple[str, ...], tuple[str, ...], tuple[Caveat, ...]]:
    """One (declaration, extension) pair's verdict — shared by every ES-DE-driven handle.

    Returns ``(verdict, entry, alternatives, sources, own caveats)``.
    Module-level for the reason :func:`_entries_from` is: the match is
    ES-DE's, not an arrangement's, and two copies of it are how "launchable
    here" and "launchable there" would silently mean different comparisons.

    An accepted extension is judged one level further (issue #66): the entry
    that would run gets :func:`_entry_reading`'s stance, and an established
    refusal flips the verdict to ``entry-not-accepted`` — the accept-list is
    the union over every entry, and the machine then does nothing at all. The
    other declared entries are judged only then, because ``alternatives`` is
    that verdict's remedy and nobody else's read.

    The unknowns split three ways and only one earns ``system-unknown``: a
    catalogue read **completely** that declares no such system. An incomplete
    read (EmuDeck's sealed bundled layer) may hide the declaration, so the
    sealed statement riding the answer is the whole claim. And a declaration
    ES-DE itself would refuse — no ``<extension>`` or no ``<command>`` — is a
    system the frontend runs without (loadConfig skips it,
    SystemData.cpp:1109-1119 @ v3.4.1), stated as unknown with the reason
    rather than matched against a list nothing consults.
    """
    if declaration is None:
        if not complete:
            return VERDICT_UNKNOWN, None, (), (), ()
        return (
            VERDICT_UNKNOWN,
            None,
            (),
            (),
            (
                Caveat(
                    CAVEAT_SYSTEM_UNKNOWN,
                    f"the catalogue was read and declares no system {system!r} — whether anything "
                    "launches this file is a question no layer here answers",
                    {"system": system},
                ),
            ),
        )
    if not declaration.extensions or not declaration.entries:
        missing = "extension" if not declaration.extensions else "command"
        return (
            VERDICT_UNKNOWN,
            None,
            (),
            (),
            (
                Caveat(
                    CAVEAT_SYSTEM_UNKNOWN,
                    f"system {system!r} is declared without a <{missing}> tag, and ES-DE skips such "
                    "a system wholesale (loadConfig, SystemData.cpp:1109-1119 @ v3.4.1) — the "
                    "frontend runs without it",
                    {"system": system},
                ),
            ),
        )
    if extension in declaration.extensions:
        match_source = (
            f"accept-list match: the file yields {extension!r} (its name from the last dot, "
            "case preserved — FileSystemUtil.cpp:630-645 @ v3.4.1) and the system's "
            "<extension> list declares that exact token (the scan compares exactly, "
            "SystemData.cpp:669)"
        )
        entry = entries[0] if entries else None
        if entry is None:
            return VERDICT_LAUNCHABLE, None, (), (match_source,), ()
        stance, entry_sources, entry_caveats = _entry_reading(
            entry, extension=extension, core_info_for=core_info_for
        )
        if stance == _ENTRY_REFUSES:
            alternatives = tuple(
                other.label
                for other in entries[1:]
                if _entry_reading(other, extension=extension, core_info_for=core_info_for)[0]
                == _ENTRY_ACCEPTS
            )
            return (
                VERDICT_ENTRY_NOT_ACCEPTED,
                entry,
                alternatives,
                (match_source, *entry_sources),
                entry_caveats,
            )
        return VERDICT_LAUNCHABLE, entry, (), (match_source, *entry_sources), entry_caveats
    record = lookup_install_first(system, extension)
    if record is not None:
        return (
            VERDICT_NEEDS_INSTALLATION,
            None,
            (),
            (f"install-first format {extension!r} for {system}: {record.statement} — {record.source}",),
            (),
        )
    return (
        VERDICT_NOT_ACCEPTED,
        None,
        (),
        (
            f"the file yields {extension!r} and the system's <extension> list "
            f"({' '.join(declaration.extensions)}) carries no such token — the comparison is "
            "exact and case-sensitive (SystemData.cpp:669 @ v3.4.1), so ES-DE never scans the "
            "file and nothing here launches it",
        ),
        (),
    )


_DERIVED_ENTRY_PROVENANCE = (
    "derived from the installed core's own systemname (.info, read live) — no catalogue "
    "declares this entry, so it carries no launch command"
)


# The context caveats that qualify the *enumeration* a derived catalogue
# answer is built on — carried onto that answer, unlike the context's
# firmware-root statements, which qualify a question this one never asked.
_ENUMERATION_CAVEAT_CODES = frozenset(
    (
        CAVEAT_INFO_PATH_UNRESOLVED,
        CAVEAT_CORE_DIR_UNRESOLVED,
        CAVEAT_CORE_ENUMERATION_INCOMPLETE,
        CAVEAT_CORE_INFO_UNREADABLE,
    )
)


def _derived_catalogue_entries(
    host: "_CatalogueHost", context: FirmwareContext, system: str
) -> tuple[tuple[EmulatorEntry, ...], tuple[Caveat, ...]]:
    """Catalogue-shaped entries for *system*, derived from the installed cores (issue #133).

    The selection is :func:`atlas.firmware.derived_core_selection` — the same
    one the firmware route uses, so the two questions can never derive
    different lists for one system. Each entry is a real
    :class:`EmulatorEntry` with the core's own ``corename`` as its label (the
    ``.so`` name where the ``.info`` states none) and an **empty command**:
    no catalogue declares one, and empty is the honest statement where any
    string would be an invention. The order is the enumeration's own
    (alphabetical by ``.so``) and claims nothing — with no catalogue and no
    user selection there is no "entry that would run", and the
    ``emulator-list-derived`` caveat says all of that in one stable code.
    """
    selected, hidden = derived_core_selection(context.cores, system)
    entries = tuple(
        EmulatorEntry(
            host,
            EmulatorSpec(
                system=system,
                label=core.corename or core.core_so,
                kind=KIND_LIBRETRO,
                core_so=core.core_so,
                command="",
                provenance=_DERIVED_ENTRY_PROVENANCE,
            ),
        )
        for core in selected
    )
    derived = Caveat(
        CAVEAT_EMULATOR_LIST_DERIVED,
        "these entries are derived from the installed cores' own systemname (.info, read live), "
        "not from a catalogue — the order claims no default, no entry carries a launch command, "
        "and a catalogue could declare a different list",
        {"system": system},
    )
    enumeration = tuple(c for c in context.caveats if c.code in _ENUMERATION_CAVEAT_CODES)
    return entries, (derived, *enumeration, *(() if hidden is None else (hidden,)))


def _firmware_with_caveats(answer: FirmwareAnswer, caveats: tuple[Caveat, ...]) -> FirmwareAnswer:
    """*answer* with *caveats* as its caveat list — ``dataclasses.replace`` behind a concrete signature."""
    return cast(FirmwareAnswer, _dc_replace(answer, caveats=caveats))


# An entry's own caveats ride the answer it qualifies, and a refusal is not one:
# it has no caveat list, and it is already the whole answer. Same shape as the
# helpers above and monomorphic for the same reason.


def _entry_savefile_with_caveats(
    outcome: SavefilePlacement | Unresolved, extra: tuple[Caveat, ...]
) -> SavefilePlacement | Unresolved:
    """*outcome* with the entry's *extra* caveats appended — refusals pass through untouched."""
    if isinstance(outcome, Unresolved) or not extra:
        return outcome
    return cast(SavefilePlacement, _dc_replace(outcome, caveats=(*outcome.caveats, *extra)))


def _entry_savestate_with_caveats(
    outcome: SavestatePlacement | Unresolved, extra: tuple[Caveat, ...]
) -> SavestatePlacement | Unresolved:
    """*outcome* with the entry's *extra* caveats appended — refusals pass through untouched."""
    if isinstance(outcome, Unresolved) or not extra:
        return outcome
    return cast(SavestatePlacement, _dc_replace(outcome, caveats=(*outcome.caveats, *extra)))


def _entry_texture_with_caveats(
    outcome: TexturePlacement | Unresolved, extra: tuple[Caveat, ...]
) -> TexturePlacement | Unresolved:
    """*outcome* with the entry's *extra* caveats appended — refusals pass through untouched."""
    if isinstance(outcome, Unresolved) or not extra:
        return outcome
    return cast(TexturePlacement, _dc_replace(outcome, caveats=(*outcome.caveats, *extra)))


def _entry_mod_with_caveats(
    outcome: ModPlacement | Unresolved, extra: tuple[Caveat, ...]
) -> ModPlacement | Unresolved:
    """*outcome* with the entry's *extra* caveats appended — refusals pass through untouched."""
    if isinstance(outcome, Unresolved) or not extra:
        return outcome
    return cast(ModPlacement, _dc_replace(outcome, caveats=(*outcome.caveats, *extra)))


_CROSSWALK_SOURCE = "platform crosswalk (platform_ids_crosswalk.json, pinned sources cited inside)"


@dataclass(frozen=True, slots=True)
class _PlatformView:
    """One handle's platform-relevant reading of its catalogue, taken once per query.

    ``declared`` maps every system this installation's systems answer lists to
    its platform tags; ``vocabulary_backed`` names the subset whose tags could
    not be read live and came from the snapshot column instead (a sealed
    catalogue's derived systems, a catalogue-less arrangement's whole list).
    ``disabled`` maps the systems the catalogue's text carries only inside XML
    comments — read where the handle can read them (the RetroDECK layers; a
    sealed bundled file cannot be scanned, so on those handles a commented
    system degrades to *absent*, which is stated in the answer's tags rather
    than silently wrong). ``caveats`` is the route's full caveat list in the
    handle's pinned order — findings first, then the catalogue-status
    statements — the same list the systems answer states.
    """

    declared: Mapping[str, tuple[str, ...]]
    vocabulary_backed: frozenset[str]
    disabled: Mapping[str, tuple[str, ...]]
    sources: tuple[str, ...]
    caveats: tuple[Caveat, ...]


def _absent_vocabulary_systems(view: _PlatformView) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``(system, snapshot tags)`` for every vocabulary system the view neither declares nor disables."""
    return tuple(
        (system, vocabulary_platform_tags(system) or ())
        for system in known_systems()
        if system not in view.declared and system not in view.disabled
    )


def _commented_map(texts: tuple[str | None, ...]) -> dict[str, tuple[str, ...]]:
    """The commented-out systems of *texts*, first sighting of a name winning."""
    disabled: dict[str, tuple[str, ...]] = {}
    for text in texts:
        if text is None:
            continue
        for name, tags in commented_out_systems(text):
            disabled.setdefault(name, tags)
    return disabled


class _CatalogueQueries:
    """The catalogue entry points, answered by every handle — including with a refusal.

    "Which emulator would launch this?" is a question about an arrangement, not
    a RetroDECK feature, so every handle answers it. RetroDECK answers it from
    its ES-DE's full catalogue; EmuDeck answers it from its ES-DE's on-disk
    layers where an ES-DE is present — stated as incomplete, because the
    bundled layer is sealed inside the AppImage. The rest state why they
    cannot, and those reasons are different claims that must not collapse into
    one empty tuple:

    - a bare RetroArch has no frontend catalogue at all — a fact about the
      arrangement, and a settled one;
    - an EmuDeck arrangement with no ES-DE on disk may still have a catalogue
      (Pegasus, Steam ROM Manager), and atlas has not established where those
      keep one. That is a statement about atlas, not about the machine, and a
      client that read it as "no emulators" would be told something nobody
      checked.
    """

    kind: str

    def standalone_firmware_token(self, command: str) -> str | None:
        """The emulator identity *command* states, for the firmware seam.

        The ES-DE default: the ``%EMULATOR_…%`` token is the catalogue's own
        identifier. EmuDeck overrides this with its launcher-route reading —
        what a command identifies is arrangement knowledge.
        """
        return emulator_token(command)

    def standalone_firmware_homes(self, command: str) -> "_XdgHomes | None":
        """The per-entry override of the context's standalone bases — none by default.

        One flatpak holds every emulator RetroDECK ships, so the pair the
        firmware context carries is right for all of them. EmuDeck overrides
        this: its launches pick a binary per entry, and which trees that
        binary reads is the variant's fact, not the arrangement's.
        """
        del command
        return None

    def _catalogue_absence(self) -> Caveat:
        raise NotImplementedError  # pragma: no cover - every handle supplies one

    def systems(self) -> SystemsAnswer:
        """Every system the frontend catalogue declares, sorted."""
        answer, version = self._systems_answer()
        return _systems_with_caveats(
            answer, (*answer.caveats, *arrangement_caveats(self.kind, observed_version=version))
        )

    def _platform_view(self) -> tuple[_PlatformView, str | None]:
        raise NotImplementedError  # pragma: no cover - every handle supplies one

    def systems_for_platform(self, vocabulary: str, value: str) -> PlatformSystemsAnswer:
        """Which systems here answer to a public platform id, and how firmly.

        The two halves of the question are answered from their two rightful
        places: what the id *corresponds to* comes from the crosswalk (world
        knowledge, pinned and cited), and whether the corresponding systems
        *are here* is read off this installation — its catalogue's own
        ``<platform>`` tags connect each system to its platform, so a system
        the user added by hand translates without any table knowing it.

        *vocabulary* is one of :data:`atlas.platforms.KNOWN_PLATFORM_VOCABULARIES`
        and anything else raises — the set is atlas's own and closed. A *value*
        no crosswalk row carries answers no platforms, no matches and the
        ``platform-unmapped`` caveat: "no platform corresponds" is an answer,
        and inventing a folder name out of the raw id is exactly the failure
        this question exists to prevent.

        Each match's status says what the consumer's next step is: a
        ``declared`` system takes content now, a ``disabled`` one exists in
        the catalogue's text and is deliberately off, an ``absent`` one is
        vocabulary knowledge only — this installation never scans a folder
        for it. The match's ``tags_source`` states whether its tags were read
        off the machine or taken from the stated build's snapshot column.
        """
        view, version = self._platform_view()
        tail = arrangement_caveats(self.kind, observed_version=version)
        resolved = platforms_for(vocabulary, value)
        if not resolved:
            unmapped = Caveat(
                CAVEAT_PLATFORM_UNMAPPED,
                "no platform in the crosswalk answers to this id — placing content under "
                "the raw id would name a folder no catalogue declares",
                {"vocabulary": vocabulary, "value": value},
            )
            return PlatformSystemsAnswer(
                vocabulary, value, caveats=(*view.caveats, unmapped, *tail)
            )
        wanted = frozenset(resolved)
        matches = [
            PlatformSystemMatch(
                system,
                PLATFORM_STATUS_DECLARED,
                view.declared[system],
                PLATFORM_TAGS_VOCABULARY
                if system in view.vocabulary_backed
                else PLATFORM_TAGS_CATALOGUE,
            )
            for system in sorted(view.declared)
            if wanted & frozenset(view.declared[system])
        ]
        matches += [
            PlatformSystemMatch(
                system, PLATFORM_STATUS_DISABLED, view.disabled[system], PLATFORM_TAGS_CATALOGUE
            )
            for system in sorted(view.disabled)
            if system not in view.declared and wanted & frozenset(view.disabled[system])
        ]
        matches += [
            PlatformSystemMatch(
                system, PLATFORM_STATUS_ABSENT, tags, PLATFORM_TAGS_VOCABULARY
            )
            for system, tags in _absent_vocabulary_systems(view)
            if wanted & frozenset(tags)
        ]
        return PlatformSystemsAnswer(
            vocabulary,
            value,
            resolved,
            tuple(matches),
            (*view.sources, _CROSSWALK_SOURCE),
            (*view.caveats, *tail),
        )

    def platform_ids(self, system: str) -> SystemPlatformsAnswer:
        """One system's platform tags and their public identities, status-qualified.

        The reverse direction: which IGDB platform, libretro database names
        and scraper ids belong to *system* — read from the system's own
        ``<platform>`` tags where this installation declares it (a commented
        block included), and from the stated build's snapshot column where it
        does not (``tags_source`` says which). A name neither this machine
        nor the vocabulary knows answers no identities and the
        ``platform-unmapped`` caveat.
        """
        view, version = self._platform_view()
        tail = arrangement_caveats(self.kind, observed_version=version)
        if system in view.declared:
            status = PLATFORM_STATUS_DECLARED
            tags = view.declared[system]
            tags_source = (
                PLATFORM_TAGS_VOCABULARY
                if system in view.vocabulary_backed
                else PLATFORM_TAGS_CATALOGUE
            )
        elif system in view.disabled:
            status, tags, tags_source = (
                PLATFORM_STATUS_DISABLED,
                view.disabled[system],
                PLATFORM_TAGS_CATALOGUE,
            )
        else:
            snapshot = vocabulary_platform_tags(system)
            if snapshot is None:
                unmapped = Caveat(
                    CAVEAT_PLATFORM_UNMAPPED,
                    "neither this installation's catalogue nor the vocabulary knows a "
                    "system of this name, so there are no platform tags to translate",
                    {"system": system},
                )
                return SystemPlatformsAnswer(
                    system,
                    PLATFORM_STATUS_ABSENT,
                    PLATFORM_TAGS_VOCABULARY,
                    caveats=(*view.caveats, unmapped, *tail),
                )
            status, tags, tags_source = (
                PLATFORM_STATUS_ABSENT,
                snapshot,
                PLATFORM_TAGS_VOCABULARY,
            )
        identities: list[PlatformIdentities] = []
        notes: list[Caveat] = []
        for tag in tags:
            if tag == "ignore":
                notes.append(
                    Caveat(
                        CAVEAT_PLATFORM_SCRAPING_IGNORED,
                        "the catalogue tags this system `ignore` — ES-DE's deliberate "
                        "opt-out from platform matching, a different fact than a "
                        "missing tag",
                        {"system": system},
                    )
                )
                continue
            row = platform_identities(tag)
            if row is None:
                notes.append(
                    Caveat(
                        CAVEAT_PLATFORM_UNKNOWN,
                        "a <platform> token outside the platform vocabulary — ES-DE "
                        "warns about and drops exactly this token, so it matches "
                        "nothing anywhere",
                        {"system": system, "token": tag},
                    )
                )
                continue
            identities.append(row)
        return SystemPlatformsAnswer(
            system,
            status,
            tags_source,
            tags,
            tuple(identities),
            (*view.sources, _CROSSWALK_SOURCE),
            (*view.caveats, *notes, *tail),
        )

    def rom_location(self, system: str) -> RomPlacement:
        """Where *system*'s ROMs live, and which extensions the frontend launches.

        Both facts are declared per system in the frontend's own catalogue and
        are read from it live — a client that recomputes either from a table of
        its own is answering from somewhere the machine cannot contradict.

        No directory is five different facts, and the codes tell them apart the
        way the catalogue question's do: the arrangement ships no catalogue, it
        has one atlas has not established, its catalogue could not be read, the
        readable part of it declares nothing while the rest is sealed away, or
        the catalogue was read and this is what it says — no such system, or a
        ``%ROMPATH%`` nothing here resolves.
        """
        answer, version = self._rom_location_answer(system)
        return _rom_placement_with_caveats(
            answer, (*answer.caveats, *arrangement_caveats(self.kind, observed_version=version))
        )

    def emulators_for(self, system: str, *, content_path: str | None = None) -> CatalogueAnswer:
        """The emulators that can launch *system*, in launch-priority order.

        The first entry is the effective default, resolved live through the
        frontend's own hierarchy: a per-game ``altemulator`` (when
        *content_path* is given and a gamelist entry matches it) outranks a
        per-system ``alternativeEmulator``, which outranks the declared order.
        A selection naming no declared entry keeps the declared order — ES-DE
        falls back the same way.

        When *content_path* is omitted and the gamelist carries per-game
        overrides, every returned entry states that as a catalogue caveat: the
        system-level answer may be wrong for exactly those games.

        No entries is five different facts, and the four
        ``emulator-catalogue-*`` codes tell them apart. None of the four means
        the catalogue was read and the frontend knows no emulator for this
        system — the only one of the five that is a statement about the
        machine. Three say why nobody could answer from a catalogue at all:
        the arrangement ships none, atlas has not established where it keeps
        one, or the one it has could not be read. The fourth (``sealed``) says
        the answer came from the readable part of a catalogue whose rest atlas
        cannot open — it is the one that may also accompany real entries, and
        an empty list under it means "nothing readable declares this", never
        "the frontend knows none".

        The test is those codes, not an empty ``caveats``: a broken
        installation states its health findings on this answer as on every
        other, so an empty caveat list is not what "read, and it declares
        nothing" looks like there.
        """
        answer, version = self._catalogue_answer(system, content_path=content_path)
        return _catalogue_with_caveats(
            answer, (*answer.caveats, *arrangement_caveats(self.kind, observed_version=version))
        )

    def launchable(self, system: str, content_path: str) -> LaunchabilityAnswer:
        """Whether *content_path* launches as *system* content here — and why not, when not.

        The positive half is a read: the frontend's own accept-list for the
        system, matched the way ES-DE matches it (the file name's token from
        its last dot, compared exactly and case-sensitively), with the entry
        that would run resolved through the same selection hierarchy
        :meth:`emulators_for` applies. The negative half is the interesting
        one, and the ``verdict`` keeps its meanings apart:

        - ``not-accepted`` — no token in the accept-list equals this file's
          extension, so ES-DE never scans it: pick another file, or another
          form of the content.
        - ``needs-installation`` — the format is real content for the platform
          and an installation step has to run before anything launches (a PSN
          ``.pkg``): recorded world knowledge, cited in the sources.
        - ``unknown`` — the system is not one this catalogue declares, or the
          catalogue could not be read: a statement about the look, never about
          the file, and never collapsed into either 'no'.

        One boundary is stated on every ``launchable`` verdict rather than
        silently crossed: the accept-list is declared per *system* and the
        command per *emulator*, so a file the list accepts may still be one
        the entry that runs cannot read (issue #66 tracks resolving that per
        entry).
        """
        answer, version = self._launchable_answer(system, content_path)
        return _launchable_with_caveats(
            answer, (*answer.caveats, *arrangement_caveats(self.kind, observed_version=version))
        )

    # The two public questions above are the whole catalogue surface, and a
    # handle overrides the two below instead: what it *answers* is its own,
    # how the answer states its arrangement's evidence is not. Splitting them
    # is what makes "every answer says what atlas has established about this
    # arrangement" a property of the surface rather than of remembering.
    #
    # Which is why the version the arrangement states about itself travels back
    # with the answer: the evidence above is weighed against it, and the handle
    # already read it — asking for it here would read the marker a second time
    # inside one query, and the two reads could disagree.

    def health(self) -> Health:
        raise NotImplementedError  # pragma: no cover - every handle answers it

    def _systems_answer(self) -> tuple[SystemsAnswer, str | None]:
        # A handle with no catalogue reads nothing to refuse, so its health is
        # this query's own single read of the sources — no snapshot to reuse
        # and none read twice. It states no version either: an arrangement
        # nobody verified has no pin to drift from.
        return SystemsAnswer(caveats=(*self.health().issues, self._catalogue_absence())), None

    def _catalogue_answer(
        self, system: str, *, content_path: str | None = None
    ) -> tuple[CatalogueAnswer, str | None]:
        return CatalogueAnswer(caveats=(*self.health().issues, self._catalogue_absence())), None

    def _launchable_answer(
        self, system: str, content_path: str
    ) -> tuple[LaunchabilityAnswer, str | None]:
        # The accept-list lives in the catalogue this arrangement does not
        # have, so the verdict is unknown for the same reason the entry list
        # is empty — the refusal is the catalogue question's own, worn by
        # this answer's shape.
        answer, version = self._catalogue_answer(system, content_path=content_path)
        return (
            LaunchabilityAnswer(
                verdict=VERDICT_UNKNOWN,
                extension=esde_extension(content_path),
                caveats=answer.caveats,
            ),
            version,
        )

    def _rom_location_answer(self, system: str) -> tuple[RomPlacement, str | None]:
        # Same refusal as the two above, for the same reason: where a system's
        # ROMs live is declared in the catalogue this arrangement does not have,
        # so the honest answer names the absence rather than a directory.
        return RomPlacement(caveats=(*self.health().issues, self._catalogue_absence())), None


class RetroDeck(_FirmwareQueries, _CatalogueQueries):
    """A RetroDECK installation — cfg is the truth, ``retrodeck.json`` is context.

    The handle is *live*: it stores only its identity (home) and the machine
    seam. Every query re-reads the governing sources — each exactly once — and
    derives all decisions from that one snapshot, so a concurrent config edit
    can never mix two revisions inside one answer (REVIEW M4).
    """

    kind = "retrodeck"
    kinds = ("retrodeck",)
    _APP_ID = RETRODECK_APP_ID

    def __init__(self, home: str, machine: Machine) -> None:
        self._home = home
        self._machine = machine

    def _marker_path(self) -> str:
        return os.path.join(self._home, RETRODECK_JSON_SUFFIX)

    def _read_marker(self) -> tuple[dict[str, Any], tuple[Caveat, ...]]:
        """One live read of ``retrodeck.json`` → (config, marker issues).

        Missing, unreadable, and invalid are distinct states — a marker that
        exists but cannot be read or parsed is a *present, broken* RetroDECK,
        never an absent one (REVIEW H10).

        A defect is scoped to what it actually costs. A ``paths`` value atlas
        cannot read as a path takes the whole snapshot down, because every root
        below is resolved from that section; a ``version`` that is not a string
        costs the version comparison and nothing else, so the config survives
        it and only the comparison falls away. Both are ``marker-invalid``: the
        finding names the key, and the marker is editable either way.
        """
        path = self._marker_path()
        result = self._machine.read_text(path)
        if result.status == READ_MISSING:
            return {}, (Caveat(HEALTH_ISSUE_MARKER_MISSING, f"marker {path} does not exist", {"path": path}),)
        if result.text is None:
            return {}, (
                Caveat(
                    HEALTH_ISSUE_MARKER_UNREADABLE,
                    f"marker {path} cannot be read as text ({result.status})",
                    {"path": path, "status": result.status},
                ),
            )
        try:
            data = json.loads(result.text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return {}, (
                Caveat(HEALTH_ISSUE_MARKER_INVALID, f"marker {path} is not a JSON object", {"path": path}),
            )
        malformed = _malformed_marker_paths(data)
        if malformed is not None:
            key, complaint = malformed
            return {}, (
                Caveat(
                    HEALTH_ISSUE_MARKER_INVALID,
                    f"marker {path}: {key} {complaint} — atlas reads it to resolve RetroDECK's "
                    "roots, so those roots fall back to their defaults instead",
                    {"path": path, "key": key},
                ),
            )
        if "version" in data and not isinstance(data["version"], str):
            return data, (
                Caveat(
                    HEALTH_ISSUE_MARKER_INVALID,
                    f"marker {path}: version is {_json_type_name(data['version'])}, not a version "
                    "string — atlas reads it to compare this installation against the version its "
                    "knowledge was verified on, so that comparison is not made",
                    {"path": path, "key": "version"},
                ),
            )
        return data, ()

    def _config_path(self, config: dict[str, Any], key: str, fallback_subdir: str) -> tuple[str, str]:
        """Resolve a RetroDECK path and its provenance from a marker snapshot.

        A sub-path key that is unset falls back under the *resolved* root
        (``rd_home_path`` or its own ``~/retrodeck`` fallback) — RetroDECK
        lays its tree out under the home it was pointed at, so the honest
        default follows the configured root, not a hard-coded one.

        Only a string is a path: :meth:`_read_marker` refuses a marker whose
        ``paths`` hold anything else, so the check here is what makes that
        guarantee local — nothing but a path ever leaves this method.
        """
        paths = config.get("paths")
        if isinstance(paths, dict):
            value: object = paths.get(key, "")
            if isinstance(value, str) and value:
                return value, f"retrodeck.json: paths.{key}"
        if not fallback_subdir:
            fallback = os.path.join(self._home, "retrodeck")
        else:
            root = self._config_path(config, "rd_home_path", "")[0]
            fallback = os.path.join(root, fallback_subdir)
        return fallback, f"default: {key} unset → {fallback}"

    def root(self) -> str:
        """The RetroDECK home directory (``rd_home_path`` or the fallback)."""
        return self._config_path(self._read_marker()[0], "rd_home_path", "")[0]

    def saves_root(self) -> str:
        """The RetroDECK saves root (``saves_path`` or the fallback)."""
        return self._config_path(self._read_marker()[0], "saves_path", "saves")[0]

    def bios_dir(self) -> str:
        """The RetroDECK BIOS directory (``bios_path`` or the fallback)."""
        return self._config_path(self._read_marker()[0], "bios_path", "bios")[0]

    def roms_dir(self) -> str | None:
        """The ROM root the frontend substitutes for ``%ROMPATH%`` — or ``None``.

        The **root**, not a system's directory: a system's own directory is
        ``rom_location(system).dir``, which is this plus whatever ``<path>`` the
        catalogue declares for it — usually the system name, and not reliably
        so, which is why the two are different questions.

        Read from ES-DE's ``ROMDirectory`` rather than ``retrodeck.json``'s
        ``roms_path``. The two agree on a stock installation and are wired one
        way — RetroDECK seds its own value into ES-DE's setting and never reads
        it back — so where a user has moved them apart, only this one is what
        the frontend launches from.

        ``None`` is a refusal, never "no ROMs here", and it is the same refusal
        :meth:`rom_location` states — for the two of its reasons that belong
        to the root: ES-DE's settings exist and could not be read, or the
        configured value is not an absolute path even after the frontend's own
        ``~`` expansion. A bare string cannot carry which, and raising is not
        this domain's grammar, so a caller who needs the reason asks
        ``rom_location(system)`` and reads its caveats.
        """
        return self._rom_root().directory

    def _health_from(self, config: dict[str, Any], marker_issues: tuple[Caveat, ...]) -> Health:
        issues = list(marker_issues)
        root = self._config_path(config, "rd_home_path", "")[0]
        if self._machine.path_kind(root) != KIND_DIRECTORY:
            issues.append(
                Caveat(HEALTH_ISSUE_ROOT_MISSING, f"root {root} is not an existing directory", {"path": root})
            )
        saves = self._config_path(config, "saves_path", "saves")[0]
        if self._machine.path_kind(saves) != KIND_DIRECTORY:
            issues.append(
                Caveat(
                    HEALTH_ISSUE_SAVES_ROOT_MISSING,
                    f"saves root {saves} is not an existing directory",
                    {"path": saves},
                )
            )
        return Health(tuple(issues))

    def health(self) -> Health:
        """Installation health — marker readable and parseable, roots present, catalogue loadable.

        The catalogue check lives here rather than in :meth:`_health_from`,
        and the reason is the one-read consistency model: the per-question
        health computation must not open sources the question itself never
        reads, so the catalogue-invalid finding rides the health question
        (its own single read here) and every answer whose question reads the
        catalogue anyway — never the others.
        """
        config, marker_issues = self._read_marker()
        health = self._health_from(config, marker_issues)
        root = self._config_path(config, "rd_home_path", "")[0]
        catalogue_invalid = self._read_catalogue(root)[3]
        issues = health.issues
        if catalogue_invalid is not None:
            issues = (*issues, catalogue_invalid)
        return Health((*issues, *self._content_tree_findings(config)))

    def _content_tree_findings(self, config: dict[str, Any]) -> tuple[Caveat, ...]:
        """The dir_prep pairs whose hub tree exists without its emulator-side link.

        Like the catalogue check above, this lives on the health question and
        nowhere else: no answer's own question reads the wiring state, so no
        answer pays for it. Every gate fails closed. A wiring table pinned to
        a version the marker does not name checks nothing — that version's
        promise was never read. A hub tree that is not a directory files
        nothing, so its pair supports no finding. And an emulator-side path
        whose ``stat`` fails is a path atlas could not read, which is never
        evidence of a broken link.

        The wired test is deliberately weak: the emulator-side path must
        resolve *into the family's hub*, not onto the exact tree this version
        pairs it with — older RetroDECK versions wired coarser trees
        (``texture_packs/Dolphin`` where today's layout is
        ``texture_packs/Dolphin/Textures``), and a link they created is hub
        wiring, not a break. What the finding states is the pair with **no**
        hub backing at all.
        """
        wiring = lookup_content_tree_wiring(self.kind)
        if wiring is None or config.get("version") != wiring.version:
            return ()
        homes = self._xdg_homes()
        bases = {
            "bios": self._config_path(config, "bios_path", "bios")[0],
            "storage": self._config_path(config, "storage_path", "storage")[0],
            "xdg-data": homes.data,
            "xdg-config": homes.config,
        }
        hub_roots = {
            "texture_packs": self._config_path(config, "texture_packs_path", "texture_packs")[0],
            "mods": self._config_path(config, "mods_path", "mods")[0],
        }
        resolved_roots = {
            family: _resolve_symlink_chain(self._machine, root)[0]
            for family, root in hub_roots.items()
        }
        findings: list[Caveat] = []
        for row in wiring.rows:
            hub_dir = os.path.join(hub_roots[row.family], row.hub)
            if self._machine.path_kind(hub_dir) != KIND_DIRECTORY:
                continue
            finding = self._content_tree_finding(
                row,
                version=wiring.version,
                hub_dir=hub_dir,
                path=os.path.join(bases[row.base], row.path),
                hub_root=resolved_roots[row.family],
            )
            if finding is not None:
                findings.append(finding)
        return tuple(findings)

    def _content_tree_finding(
        self, row: WiringRow, *, version: str, hub_dir: str, path: str, hub_root: str | None
    ) -> Caveat | None:
        """One pair's verdict — ``None`` exactly when the path resolves into the hub."""
        resolved, links = _resolve_symlink_chain(self._machine, path)
        if (
            resolved is not None
            and hub_root is not None
            and (resolved == hub_root or resolved.startswith(hub_root + "/"))
        ):
            # Wired — even when the target does not exist yet: creating the
            # hub side brings a dead link to life, so the pair still routes
            # everything filed in the hub to the emulator.
            return None
        if links:
            target = resolved if resolved is not None else links[0][1]
            return _content_tree_unwired_finding(
                row, version=version, hub_dir=hub_dir, path=path, problem="diverted", target=target
            )
        kind = self._machine.path_kind(path)
        if kind == KIND_INACCESSIBLE:
            return None
        problem = "missing" if kind == KIND_MISSING else "not-a-link"
        return _content_tree_unwired_finding(
            row, version=version, hub_dir=hub_dir, path=path, problem=problem, target=None
        )

    def _retroarch_config_dir(self) -> str:
        return os.path.join(self._home, ".var", "app", self._APP_ID, "config", "retroarch")

    def _sandbox(self) -> _Sandbox:
        return _Sandbox(self._machine, self._home, self._APP_ID, expansion_home=self._home)

    # ES-DE catalogue — read live: bundled file in the Flatpak deployment,
    # user overlay under <rd_home>/ES-DE/custom_systems (observed layout).
    _ESDE_BUNDLED_SANDBOX = "/app/retrodeck/components/es-de/share/es-de/resources/systems/linux/es_systems.xml"

    def _overlay_path(self, root: str) -> str:
        return os.path.join(root, "ES-DE", "custom_systems", _ES_SYSTEMS_XML)

    def _catalogue_exclusive(self, root: str, system: str | None = None) -> tuple[Caveat, ...]:
        return _catalogue_exclusive_caveat(self._overlay_path(root), system)

    def _read_catalogue(
        self, root: str
    ) -> tuple[dict[str, SystemDeclaration], bool, bool, Caveat | None]:
        """The merged ES-DE catalogue → ``(by_system, read, exclusive, invalid)``."""
        by_system, read, exclusive, invalid, _ = self._read_catalogue_full(root)
        return by_system, read, exclusive, invalid

    def _read_catalogue_full(self, root: str) -> tuple[
        dict[str, SystemDeclaration], bool, bool, Caveat | None, dict[str, tuple[str, ...]]
    ]:
        """The merged ES-DE catalogue → ``(by_system, read, exclusive, invalid, disabled)``.

        ``read`` is not a detail: an empty catalogue because the shipped
        ``es_systems.xml`` was unreadable says nothing about which emulators
        exist, while an empty *lookup* in a catalogue that was read says the
        frontend knows none for that system. The custom overlay is genuinely
        optional, so on the merged path only the bundled layer decides.

        The custom layer is read first because it can end the reading: a
        document-level ``<loadExclusive/>`` there makes ES-DE skip the
        bundled file wholesale (``SystemData.cpp:858-895`` @ v3.4.1 — the tag
        is honored only in ``configPaths.front()``, which is the custom file
        exactly when one exists, and RetroDECK deploys its stub always). So
        atlas does not read the bundled file either: what it declares is not
        in force, and ``exclusive`` — the catalogue then being the custom
        layer alone, complete — is the statement every consumer rides. A tag
        in the *bundled* layer is ignored the way ES-DE ignores it (the
        LogWarning branch, ``:886-895``).

        ``invalid`` is the hardest state (issue #100): a layer atlas read and
        ES-DE cannot load aborts the frontend's whole ``loadConfig``, so the
        catalogue in force is **empty** — read, exclusive of nothing, with
        the health finding naming the file. It is set only where the file's
        bytes were read: a file atlas cannot read may parse fine for the
        frontend, and stays the unread state it always was.

        ``disabled`` (issue #68) is the commented-out systems of the same
        texts this read already holds — the layers in force only, from the
        same single read (a second read could see a different machine): an
        exclusive overlay's comments alone, otherwise the bundled file's and
        the overlay's. An invalid layer contributes nothing — a file the
        frontend refuses wholesale has no "deliberately off" blocks, only an
        aborted load.
        """
        empty: dict[str, SystemDeclaration] = {}
        no_disabled: dict[str, tuple[str, ...]] = {}
        custom = CatalogueLayer(systems={})
        overlay_path = self._overlay_path(root)
        custom_text = self._machine.read_text(overlay_path).text
        if custom_text is not None:
            custom = parse_es_systems(custom_text, provenance="es_systems.xml (custom_systems overlay)")
        if custom.invalid is not None:
            return empty, True, False, _catalogue_invalid_finding(overlay_path, custom.invalid), no_disabled
        if custom.load_exclusive:
            return dict(custom.systems), True, True, None, _commented_map((custom_text,))
        bundled = CatalogueLayer(systems={})
        read = False
        bundled_text: str | None = None
        bundled_path = self._sandbox().bundled(self._ESDE_BUNDLED_SANDBOX)
        if bundled_path is not None:
            text = self._machine.read_text(bundled_path).text
            if text is not None:
                bundled = parse_es_systems(text, provenance="es_systems.xml (bundled)")
                if bundled.invalid is not None:
                    return empty, True, False, _catalogue_invalid_finding(bundled_path, bundled.invalid), no_disabled
                # Read AND parsed. RetroDECK ships the custom overlay fully
                # commented out inside an empty <systemList/>, so it parses to
                # zero systems by design and can never stand in for this.
                read = True
                bundled_text = text
        return (
            merge_layers(bundled.systems, custom.systems),
            read,
            False,
            None,
            _commented_map((bundled_text, custom_text)),
        )

    _CATALOGUE_SOURCE = "ES-DE catalogue read live (es_systems.xml, bundled + custom_systems overlay)"
    _ROM_DIRECTORY_SOURCE = "ES-DE ROMDirectory read live (es_settings.xml)"

    # ES-DE keeps its settings in its app-data tree under the Flatpak's config
    # directory, NOT under the RetroDECK home the catalogue overlay lives in —
    # the two diverge on an SD-card install, where <rd_home>/ES-DE/settings/
    # does not exist at all (observed 2026-08-08).
    _ESDE_SETTINGS_SUFFIX = os.path.join("ES-DE", "settings", "es_settings.xml")
    _ROM_DIRECTORY_SETTING = "ROMDirectory"

    def _esde_config_home(self) -> str:
        """The app's config home — what the frontend is launched with as its ``--home``.

        RetroDECK's only path to the frontend is
        ``components/es-de/component_launcher.sh:10``::

            exec "$component_path/bin/es-de" --home "${XDG_CONFIG_HOME}" "$@"

        and under Flatpak ``XDG_CONFIG_HOME`` is the per-app config directory,
        which is the tree this handle already reads the settings out of. So one
        path answers two questions: where the settings are, and what the
        frontend's home-relative defaults are relative to.
        """
        return os.path.join(self._home, ".var", "app", self._APP_ID, "config")

    def _esde_settings_path(self) -> str:
        return os.path.join(self._esde_config_home(), self._ESDE_SETTINGS_SUFFIX)

    def _rom_directory(self) -> tuple[str | None, str | None]:
        """The configured ``ROMDirectory``, and the status that stopped the reading.

        The value comes from ``es_settings.xml`` rather than from
        ``retrodeck.json``'s ``roms_path``, and the difference is the boundary
        rule, not pedantry: ``ROMDirectory`` is the setting ES-DE actually
        substitutes, while ``roms_path`` is RetroDECK's bookkeeping about the
        same tree. They agree on a stock installation and are two different
        files a user can move apart, and only one of them is what the frontend
        reads. Same shape as this handle's cfg-over-marker rule everywhere else.

        Three outcomes, and collapsing any two of them states a directory that
        is not in force. *Missing* is a reading — there is no file, so there is
        no configured value, and the frontend's own default is what applies
        (:meth:`_default_rom_directory`); so is a file that parses and sets the
        key empty or not at all. Both answer ``(None, None)``. *Unreadable*,
        *invalid-text* and *unparseable* are not readings: the file is there,
        ES-DE reads it fine, and what it says could be anything — so those
        answer the status, and no directory, rather than the default a file
        nobody could read has no claim to.

        The status rather than the caveat, because the two callers label it
        differently and only one of them has a system to name.
        """
        result = self._machine.read_text(self._esde_settings_path())
        if result.status == READ_MISSING:
            return None, None
        if result.text is None:
            return None, result.status
        settings = parse_es_settings(result.text)
        if settings is None:
            return None, _SETTINGS_UNPARSEABLE
        return settings.get(self._ROM_DIRECTORY_SETTING) or None, None

    def _rom_root(self) -> _RomRoot:
        """What ES-DE substitutes for ``%ROMPATH%`` — the ROM root, before any ``<path>``.

        The one chain every ROM-directory answer on this handle resolves
        through: :meth:`roms_dir` *is* this root, and :meth:`_esde_system_dir`
        is this root with the catalogue's declaration applied. Keeping them one
        chain is the point — two would be two rules about the same tree, and
        the day they disagreed atlas would be contradicting itself.

        A configured value carrying ``~`` is expanded the way the frontend
        expands it — every occurrence, as text
        (:func:`atlas.esde.expand_home_path`; the call is
        ``FileData.cpp:289``, ES-DE v3.4.1) — against ES-DE's own home, which
        on this arrangement is the launcher's ``--home "${XDG_CONFIG_HOME}"``
        (:meth:`_esde_config_home`), not the user's. That is the same home
        the unset default derives from, and no Flatpak override can move it:
        flatpak force-pins ``XDG_CONFIG_HOME`` to the per-app config
        directory after applying every override (flatpak 1.16.6,
        flatpak-context.c:3158-3187 via flatpak-run.c:3574, against the
        override env applied at :3352; flatpak-run(1); flatpak/flatpak#4529
        closed as not planned). Both home-derived resolutions therefore
        resolve on every machine. The absoluteness check runs on the
        *expanded* value — the raw one is what a refusal names, because the
        setting's own text is what a user edits.
        """
        configured, unreadable = self._rom_directory()
        if unreadable is not None:
            return _RomRoot(unreadable=unreadable)
        sources = (self._ROM_DIRECTORY_SOURCE,)
        if configured is None:
            return _RomRoot(directory=self._default_rom_directory(), sources=sources)
        expanded = configured
        if "~" in configured:
            expanded = expand_home_path(configured, self._esde_config_home())
        if not expanded.startswith("/"):
            return _RomRoot(not_absolute=configured, sources=sources)
        return _RomRoot(directory=expanded, sources=sources)

    def _esde_system_dir(
        self, by_system: Mapping[str, SystemDeclaration], system: str
    ) -> _RomDirectory:
        """Where ES-DE puts *system*'s ROMs: the root with the catalogue's ``<path>`` applied.

        Both the answer to "where do this system's ROMs live" and the anchor
        per-game gamelist entries are matched against, because they are the
        same directory — ES-DE reads a gamelist's ``./Name.ext`` relative to
        the very ``startPath`` it built here. Anchoring any other way means
        matching overrides against a directory the frontend does not launch
        from, which is how an override goes missing on a machine whose two ROM
        paths have drifted apart.
        """
        declaration = by_system.get(system)
        if declaration is None or declaration.rom_path is None:
            return _RomDirectory(caveats=_rom_path_undeclared_caveat(system, declaration))
        declared = declaration.rom_path
        root = self._rom_root()
        if root.unreadable is not None:
            return _RomDirectory(
                caveats=_settings_unreadable_caveat(
                    system, self._esde_settings_path(), root.unreadable
                )
            )
        if root.not_absolute is not None:
            return _RomDirectory(
                sources=root.sources,
                caveats=_rom_path_unresolved_caveat(system, declared, root.not_absolute),
            )
        resolved = resolve_rom_path(declared, root.directory)
        if resolved is None:
            # Unreachable as the branches above stand: the root is absolute
            # here and the declaration is non-empty, which is every way
            # resolve_rom_path refuses. Stated rather than dropped, because a
            # directory that silently became None is the one answer this
            # question must never give.
            return _RomDirectory(
                sources=root.sources,
                caveats=_rom_path_unresolved_caveat(system, declared, root.directory or ""),
            )
        return _RomDirectory(directory=resolved, sources=root.sources)

    def _default_rom_directory(self) -> str:
        """Where the frontend looks when ``ROMDirectory`` is unset — resolved, not asserted.

        ES-DE falls back on ``<home>/ROMs/`` when the setting is empty
        (``es-app/src/FileData.cpp::getROMDirectory()``, ES-DE 3.4.1,
        ``:271-305`` with the empty-setting branch at ``:283-284``; RetroDECK
        ships the ``RetroDECK/ES-DE`` fork with that function unmodified at the
        pinned build), and its home is not the user's: the launcher passes
        ``--home "${XDG_CONFIG_HOME}"`` unconditionally, which outranks both
        ``portable.txt`` and ``$HOME``. That makes the branch reachable and its
        value knowable, which is the whole difference between resolving and
        guessing — the empty setting is the state RetroDECK's own shipped
        template is in before its first ``sed``.

        Resolving is not asserting the directory exists. Nothing here stats it;
        an absent one is the ordinary missing-directory state every other root
        is in, and the caller's own treatment applies unchanged.
        """
        return os.path.join(self._esde_config_home(), "ROMs")

    def _cfg_sandbox(self) -> tuple[_Sandbox, tuple[str, ...]]:
        """The sandbox a cfg read resolves through — :func:`_flatpak_cfg_sandbox` for this app.

        The ``~`` seam is the override files' one consequence on this handle,
        because every file it reads is keyed off ``XDG_CONFIG_HOME`` by
        RetroDECK's own scripts (``all_vars.sh:4``, retroarch
        ``component_functions.sh:3``, es-de ``component_launcher.sh:10``),
        and flatpak pins those variables against every override.
        """
        return _flatpak_cfg_sandbox(self._machine, self._home, self._APP_ID)

    def _systems_answer(self) -> tuple[SystemsAnswer, str | None]:
        """Every system the catalogue declares, sorted — and the version that read stated.

        The findings come from the marker snapshot this query already read, so
        the answer's health, its roots and the version its evidence is weighed
        against are one revision of the file.
        """
        config, marker_issues = self._read_marker()
        findings = self._health_from(config, marker_issues).issues
        root = self._config_path(config, "rd_home_path", "")[0]
        by_system, read, exclusive, catalogue_invalid = self._read_catalogue(root)
        invalid = (catalogue_invalid,) if catalogue_invalid is not None else ()
        version = _marker_version(config)
        if not read:
            return SystemsAnswer(caveats=(*findings, *_catalogue_unread_caveat())), version
        status = self._catalogue_exclusive(root) if exclusive else ()
        return (
            SystemsAnswer(
                tuple(sorted(by_system)),
                (_CATALOGUE_SOURCE_EXCLUSIVE if exclusive else self._CATALOGUE_SOURCE,),
                (*findings, *invalid, *status),
            ),
            version,
        )

    def _platform_view(self) -> tuple[_PlatformView, str | None]:
        """RetroDECK's platform reading — live tags per declared system, commented blocks scanned.

        The same snapshot discipline as :meth:`_systems_answer`: one read of
        the marker, one of the catalogue — the disabled map comes out of that
        same read (:meth:`_read_catalogue_full`), never a second one.
        """
        config, marker_issues = self._read_marker()
        findings = self._health_from(config, marker_issues).issues
        root = self._config_path(config, "rd_home_path", "")[0]
        by_system, read, exclusive, catalogue_invalid, disabled = self._read_catalogue_full(root)
        version = _marker_version(config)
        if not read:
            view = _PlatformView(
                {}, frozenset(), {}, (), (*findings, *_catalogue_unread_caveat())
            )
            return view, version
        invalid = (catalogue_invalid,) if catalogue_invalid is not None else ()
        status = self._catalogue_exclusive(root) if exclusive else ()
        view = _PlatformView(
            {name: declaration.platforms for name, declaration in by_system.items()},
            frozenset(),
            disabled,
            (_CATALOGUE_SOURCE_EXCLUSIVE if exclusive else self._CATALOGUE_SOURCE,),
            (*findings, *invalid, *status),
        )
        return view, version

    def _gamelist_selections_at(self, root: str, system: str) -> GamelistSelections:
        gamelist_path = os.path.join(root, "ES-DE", "gamelists", system, "gamelist.xml")
        text = self._machine.read_text(gamelist_path).text
        if text is None:
            return GamelistSelections(system_label=None, per_game={})
        return parse_gamelist(text)

    def gamelist_selections(self, system: str) -> GamelistSelections:
        config, _ = self._read_marker()
        return self._gamelist_selections_at(self._config_path(config, "rd_home_path", "")[0], system)

    def _catalogue_answer(
        self, system: str, *, content_path: str | None = None
    ) -> tuple[CatalogueAnswer, str | None]:
        """RetroDECK's own catalogue answer — one snapshot of the ES-DE sources.

        The contract this fills in is on
        :meth:`_CatalogueQueries.emulators_for`; what is RetroDECK's alone is
        where the answer comes from: the marker, the bundled ``es_systems.xml``
        plus its ``custom_systems`` overlay, and the system's gamelist, each
        read once here and handed to the entry assembly together (REVIEW M4).
        The marker's version travels back with the answer for the same reason.
        """
        config, marker_issues = self._read_marker()
        findings = self._health_from(config, marker_issues).issues
        root = self._config_path(config, "rd_home_path", "")[0]
        by_system, read, exclusive, catalogue_invalid = self._read_catalogue(root)
        invalid = (catalogue_invalid,) if catalogue_invalid is not None else ()
        version = _marker_version(config)
        if not read:
            return (
                CatalogueAnswer(caveats=(*findings, *_catalogue_unread_caveat(system))),
                version,
            )
        status = self._catalogue_exclusive(root, system) if exclusive else ()
        # The anchor is only consulted where a content path was named, so only
        # that query pays ES-DE's settings read — and only that query can be
        # told the anchor failed, which is the whole reason its caveats join.
        anchor = (
            self._esde_system_dir(by_system, system)
            if content_path is not None
            else _NO_ANCHOR_NEEDED
        )
        return (
            CatalogueAnswer(
                _entries_from(
                    self,
                    _declared_entries(by_system, system),
                    self._gamelist_selections_at(root, system),
                    system_roms_dir=anchor.directory,
                    content_path=content_path,
                ),
                (_CATALOGUE_SOURCE_EXCLUSIVE if exclusive else self._CATALOGUE_SOURCE,),
                (*findings, *invalid, *status, *anchor.caveats),
            ),
            version,
        )

    def _launchable_answer(
        self, system: str, content_path: str
    ) -> tuple[LaunchabilityAnswer, str | None]:
        """RetroDECK's launchability answer — the same snapshot its catalogue answer takes.

        The marker, both catalogue layers and the system's gamelist are each
        read once here; ``complete`` is always true for this handle, because
        its bundled layer is a readable file in the Flatpak deployment — an
        undeclared system is therefore genuinely unknown to the frontend,
        never possibly-sealed-away.
        """
        extension = esde_extension(content_path)
        config, marker_issues = self._read_marker()
        findings = self._health_from(config, marker_issues).issues
        root = self._config_path(config, "rd_home_path", "")[0]
        by_system, read, exclusive, catalogue_invalid = self._read_catalogue(root)
        invalid = (catalogue_invalid,) if catalogue_invalid is not None else ()
        version = _marker_version(config)
        if not read:
            return (
                LaunchabilityAnswer(
                    verdict=VERDICT_UNKNOWN,
                    extension=extension,
                    caveats=(*findings, *_catalogue_unread_caveat(system)),
                ),
                version,
            )
        status = self._catalogue_exclusive(root, system) if exclusive else ()
        anchor = self._esde_system_dir(by_system, system)
        entries = _entries_from(
            self,
            _declared_entries(by_system, system),
            self._gamelist_selections_at(root, system),
            system_roms_dir=anchor.directory,
            content_path=content_path,
        )
        declaration = by_system.get(system)
        core_reader = _EntryCoreReader(
            self._machine,
            os.path.join(self._home, RETRODECK_CFG_SUFFIX),
            self._cfg_sandbox,
        )
        verdict, entry, alternatives, sources, own = _launchability_verdict(
            system=system,
            extension=extension,
            declaration=declaration,
            entries=entries,
            complete=True,
            core_info_for=core_reader,
        )
        return (
            LaunchabilityAnswer(
                verdict=verdict,
                extension=extension,
                accepted=declaration.extensions if declaration is not None else (),
                entry=entry,
                alternatives=alternatives,
                sources=(
                    _CATALOGUE_SOURCE_EXCLUSIVE if exclusive else self._CATALOGUE_SOURCE,
                    *core_reader.sources,
                    *sources,
                ),
                caveats=(*findings, *invalid, *status, *own, *anchor.caveats),
            ),
            version,
        )

    def _rom_location_answer(self, system: str) -> tuple[RomPlacement, str | None]:
        """RetroDECK's ROM placement — the catalogue's declaration, resolved ES-DE's way.

        One snapshot again: the marker, both catalogue layers and ES-DE's
        settings are each read once here. The extensions survive an unresolved
        directory on purpose — which files launch is declared in the same
        element and does not depend on where they sit, so refusing to state
        them would throw away a fact atlas holds.
        """
        config, marker_issues = self._read_marker()
        findings = self._health_from(config, marker_issues).issues
        root = self._config_path(config, "rd_home_path", "")[0]
        by_system, read, exclusive, catalogue_invalid = self._read_catalogue(root)
        invalid = (catalogue_invalid,) if catalogue_invalid is not None else ()
        version = _marker_version(config)
        if not read:
            return (
                RomPlacement(caveats=(*findings, *_catalogue_unread_caveat(system))),
                version,
            )
        status = self._catalogue_exclusive(root, system) if exclusive else ()

        # A source names a reading this answer rests on, so the settings file
        # joins the list only where the resolution actually read it — which is
        # every outcome except the two that never opened it or opened it and
        # failed. The resolution reports that itself rather than being asked.
        declaration = by_system.get(system)
        resolved = self._esde_system_dir(by_system, system)
        placement = RomPlacement(
            extensions=() if declaration is None else declaration.extensions,
            sources=(
                _CATALOGUE_SOURCE_EXCLUSIVE if exclusive else self._CATALOGUE_SOURCE,
                *resolved.sources,
            ),
            caveats=(*findings, *invalid, *status, *resolved.caveats),
        )
        if resolved.directory is None:
            return placement, version
        # Dropping resolved.caveats here is safe only because every branch of
        # _esde_system_dir that carries one also answers directory=None, so a
        # resolved directory has nothing to say. The day one of them resolves
        # AND caveats, this line starts swallowing it.
        #
        # The same link view every placement answers with, from the one helper
        # all of them share — a fourth symlink walk is exactly what the seam's
        # three existing ones warn against.
        physical_dir, link_caveats = _link_view(self._machine, resolved.directory)
        return (
            _dc_replace(
                placement,
                dir=resolved.directory,
                physical_dir=physical_dir,
                caveats=(*findings, *status, *link_caveats),
            ),
            version,
        )

    def _query_from(
        self,
        config: dict[str, Any],
        marker_issues: tuple[Caveat, ...],
        *,
        content_path: str | None,
        core_so: str | None,
        system: str | None = None,
        extra_caveats: tuple[Caveat, ...] = (),
    ) -> _SaveQuery:
        """The placement question over a marker snapshot this query already read.

        Which family it is asked about is the resolver's business, not the
        query's: savefiles and savestates are governed by the same cfg, the same
        override chain and the same core, so one question object serves both.

        The sandbox arrives with its ``~`` base composed from the override
        files (:func:`_flatpak_query_context`) — the cfg-reading queries' one
        read of those files — and every resolution in the query goes through
        that one sandbox, the core path included, so no two parts of one
        answer can read the same cfg value against different homes. The same
        context's filesystem tables ride along as the revocation check the
        save resolvers apply to their final directory (issue #103).
        """
        health = self._health_from(config, marker_issues)
        global_cfg_path = os.path.join(self._home, RETRODECK_CFG_SUFFIX)
        global_text = self._machine.read_text(global_cfg_path).text
        version = _marker_version(config)
        context = _flatpak_query_context(self._machine, self._home, self._APP_ID)
        sandbox = context.sandbox
        return _SaveQuery(
            sandbox=sandbox,
            global_cfg_path=global_cfg_path,
            global_text=global_text,
            cfg_label=RETROARCH_CFG,
            override_config_dir=os.path.join(self._retroarch_config_dir(), "config"),
            defaults=UPSTREAM_DEFAULTS,
            content_path=content_path,
            core_so=core_so,
            core_path_resolver=lambda so: _core_path_from(sandbox, global_text, so),
            arrangement="retrodeck",
            arrangement_version=version,
            system=system,
            extra_sources=context.sources,
            extra_caveats=(
                *extra_caveats,
                *health.issues,
                *arrangement_caveats(self.kind, observed_version=version),
            ),
            revocation=context.revocation,
        )

    def _savefile_location_from(
        self,
        config: dict[str, Any],
        marker_issues: tuple[Caveat, ...],
        *,
        content_path: str | None,
        core_so: str | None,
        system: str | None = None,
        extra_caveats: tuple[Caveat, ...] = (),
    ) -> SavefilePlacement | Unresolved:
        return _retroarch_savefile_location(
            self._machine,
            self._query_from(
                config,
                marker_issues,
                content_path=content_path,
                core_so=core_so,
                system=system,
                extra_caveats=extra_caveats,
            ),
        )

    def _savestate_location_from(
        self,
        config: dict[str, Any],
        marker_issues: tuple[Caveat, ...],
        *,
        content_path: str | None,
        core_so: str | None,
        extra_caveats: tuple[Caveat, ...] = (),
    ) -> SavestatePlacement | Unresolved:
        return _retroarch_savestate_location(
            self._machine,
            self._query_from(
                config,
                marker_issues,
                content_path=content_path,
                core_so=core_so,
                extra_caveats=extra_caveats,
            ),
        )

    def savefile_location(
        self,
        *,
        content_path: str | None = None,
        core_so: str | None = None,
        system: str | None = None,
    ) -> SavefilePlacement | Unresolved:
        """Where this RetroDECK's RetroArch keeps the save for *content_path* under *core_so*.

        ``core_so`` is the core's ``.so`` basename (e.g.
        ``"mupen64plus_next_libretro.so"``) or a full path; atlas resolves
        ``library_name`` from the binary. ``system`` is the content's system in
        ES-DE's vocabulary, which is what keys a core's recorded file set. All
        three are optional — missing ones leave holes and stated caveats, never
        guesses.
        """
        config, marker_issues = self._read_marker()
        return self._savefile_location_from(
            config,
            marker_issues,
            content_path=content_path,
            core_so=core_so,
            system=system,
        )

    def savestate_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> SavestatePlacement | Unresolved:
        """Where this RetroDECK's RetroArch keeps the savestates for *content_path*.

        The savefile question's twin, taking the same two optional arguments and
        answering off the same configs — through the savestate quartet of keys
        instead of the savefile one.
        """
        config, marker_issues = self._read_marker()
        return self._savestate_location_from(
            config,
            marker_issues,
            content_path=content_path,
            core_so=core_so,
        )

    def _screenshot_location_from(
        self,
        config: dict[str, Any],
        marker_issues: tuple[Caveat, ...],
        *,
        content_path: str | None,
        core_so: str | None,
        extra_caveats: tuple[Caveat, ...] = (),
    ) -> ScreenshotPlacement | Unresolved:
        return _retroarch_screenshot_location(
            self._machine,
            self._query_from(
                config,
                marker_issues,
                content_path=content_path,
                core_so=core_so,
                extra_caveats=extra_caveats,
            ),
        )

    def screenshot_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> ScreenshotPlacement | Unresolved:
        """Where this RetroDECK's RetroArch writes screenshots for *content_path*.

        Both arguments are optional: the core matters only because a per-core
        or per-game override can move the screenshot keys, and without content
        the content-rooted answers keep their hole.
        """
        config, marker_issues = self._read_marker()
        return self._screenshot_location_from(
            config, marker_issues, content_path=content_path, core_so=core_so
        )

    def _texture_pack_location_from(
        self,
        config: dict[str, Any],
        marker_issues: tuple[Caveat, ...],
        *,
        content_path: str | None,
        core_so: str | None,
        extra_caveats: tuple[Caveat, ...] = (),
    ) -> TexturePlacement | Unresolved:
        return _retroarch_texture_pack_location(
            self._machine,
            self._query_from(
                config,
                marker_issues,
                content_path=content_path,
                core_so=core_so,
                extra_caveats=extra_caveats,
            ),
        )

    def texture_pack_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> TexturePlacement | Unresolved:
        """Where this RetroDECK's *core_so* reads texture packs from.

        RetroDECK is the arrangement this question was shaped by: it links each
        emulator's own texture directory into one shared tree, so ``dir`` is the
        path the emulator opens and ``physical_dir`` the tree the bytes are
        really in — the two truthful answers a ``dir_prep`` install produces, the
        same pair the save routes already report.

        The link is not what atlas reads the location *from*. The directory
        comes from the root RetroArch hands the core plus the fragment the core
        itself appends; whether an arrangement has redirected that directory is
        then an observation on top, which is why a machine that never ran
        RetroDECK's setup still answers.
        """
        config, marker_issues = self._read_marker()
        return self._texture_pack_location_from(
            config,
            marker_issues,
            content_path=content_path,
            core_so=core_so,
        )

    def _mod_location_from(
        self,
        config: dict[str, Any],
        marker_issues: tuple[Caveat, ...],
        *,
        content_path: str | None,
        core_so: str | None,
        extra_caveats: tuple[Caveat, ...] = (),
    ) -> ModPlacement | Unresolved:
        return _retroarch_mod_location(
            self._machine,
            self._query_from(
                config,
                marker_issues,
                content_path=content_path,
                core_so=core_so,
                extra_caveats=extra_caveats,
            ),
        )

    def mod_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> ModPlacement | Unresolved:
        """Where this RetroDECK's *core_so* reads mods from.

        The arrangement that links every emulator's mod directory into one
        shared hub, so each tree's ``dir`` is the path the emulator opens and
        its ``physical_dir`` the directory the bytes are really in — per tree,
        because the hub links them one by one.
        """
        config, marker_issues = self._read_marker()
        return self._mod_location_from(
            config, marker_issues, content_path=content_path, core_so=core_so
        )

    def soft_patch_candidates(
        self, content_path: str, *, core_so: str | None = None
    ) -> SoftPatchAnswer | Unresolved:
        """Which patch files RetroDECK's RetroArch would apply to *content_path*.

        The one arrangement whose shipped RetroArch has been read for this, so
        the candidates come back with each format's ``attempted`` decided rather
        than unestablished — and with the version pin that retires the claim
        when the shipped build moves.
        """
        config, marker_issues = self._read_marker()
        return _retroarch_soft_patch_candidates(
            self._machine,
            self._query_from(config, marker_issues, content_path=content_path, core_so=core_so),
            build=lookup_soft_patch_build(self.kind),
        )

    def _firmware_context_from(
        self,
        config: dict[str, Any],
        marker_issues: tuple[Caveat, ...],
    ) -> FirmwareContext:
        """The firmware context over a marker snapshot this query already read.

        Health comes from that same snapshot rather than from a fresh
        :meth:`health` call, so the findings an answer states and the roots it
        resolved were read from one revision of ``retrodeck.json``.

        The sandbox arrives with its ``~`` base composed from the override
        files (:meth:`_cfg_sandbox`) — this context's one read of those files,
        and the ``system_directory`` read below goes through the same sandbox
        the source line describes.
        """
        sandbox, environment_sources = self._cfg_sandbox()
        return _retroarch_firmware_context(
            sandbox=sandbox,
            global_text=self._machine.read_text(os.path.join(self._home, RETRODECK_CFG_SUFFIX)).text,
            cfg_label=RETROARCH_CFG,
            retroarch_config_dir=self._retroarch_config_dir(),
            findings=self._health_from(config, marker_issues).issues,
            arrangement_version=_marker_version(config),
            extra_sources=environment_sources,
            standalone_homes=self._xdg_homes(),
            standalone_sandbox=self._sandbox(),
        )

    def _read_firmware_context(self) -> FirmwareContext:
        config, marker_issues = self._read_marker()
        return self._firmware_context_from(config, marker_issues)

    def firmware_for_system(self, system: str, *, verify: bool = False) -> FirmwareAnswer:
        """Which emulators RetroDECK offers for *system*, and what each of them wants.

        *system* is the ES-DE system name (``"gb"``, ``"dreamcast"``), the same
        vocabulary :meth:`emulators_for` speaks — the catalogue is the
        enumeration, so an emulator whose core is not installed and a
        standalone emulator both appear, stated as such instead of silently
        dropped. Whether that catalogue could be read travels with it: an
        unreadable ``es_systems.xml`` must never come out as "this machine has
        no emulator for that system".

        The catalogue is the one :meth:`emulators_for` answers from, assembled
        from this query's own snapshot: marker and both catalogue layers are
        read once here and handed on, never re-read.
        """
        config, marker_issues = self._read_marker()
        root = self._config_path(config, "rd_home_path", "")[0]
        by_system, read, exclusive, catalogue_invalid = self._read_catalogue(root)
        catalogue = Catalogue(
            entries=_firmware_catalogue_entries(
                self, by_system, system, self._gamelist_selections_at(root, system)
            ),
            read=read,
        )
        # The marker this query already read builds the context too — asking
        # for a fresh one would read retrodeck.json twice inside one answer.
        # The override files follow the same rule: read once, by the context.
        context = self._stated(self._firmware_context_from(config, marker_issues))
        answer = _resolve_for_system(
            self._machine, context, system=system, catalogue=catalogue, verify=verify
        )
        # The catalogue-status statements ride only answers the catalogue
        # informed: an own spelling is answered from the cores, so the
        # resolver never looks at the catalogue for it. They land where the
        # resolver puts its own — right after the context's caveats (the
        # answer is (*context.caveats, *its own), per its contract).
        status = (
            *(() if catalogue_invalid is None else (catalogue_invalid,)),
            *(self._catalogue_exclusive(root, system) if exclusive else ()),
        )
        if not status or system in SYSTEMS_WITHOUT_CATALOGUE_ID:
            return answer
        index = len(context.caveats)
        return _firmware_with_caveats(
            answer,
            (*answer.caveats[:index], *status, *answer.caveats[index:]),
        )

    def _entry_caveats_for(
        self,
        config: dict[str, Any],
        spec: EmulatorSpec,
        content_path: str,
    ) -> tuple[Caveat, ...]:
        """What the gamelist says about *this* game being launched by *this* entry.

        Costs the catalogue and ES-DE's settings, and only on the content
        branch: the anchor is the system's ``<path>`` resolved against
        ``ROMDirectory``, and the catalogue is the only thing that declares a
        ``<path>``. There is no cheaper faithful route — the previous one was
        cheaper by reading the wrong file.

        The same fact governs both placement questions, because it is about
        which emulator runs at all: an entry that would not launch this game
        keeps neither its saves nor its states.
        """
        root = self._config_path(config, "rd_home_path", "")[0]
        selections = self._gamelist_selections_at(root, spec.system)
        by_system, read, exclusive, catalogue_invalid = self._read_catalogue(root)
        status = (
            *(() if catalogue_invalid is None else (catalogue_invalid,)),
            *(self._catalogue_exclusive(root, spec.system) if exclusive else ()),
        )
        # A catalogue nobody could read declares nothing, which is not the
        # same fact as a catalogue that was read and declares no <path> for
        # this system — and handing the empty snapshot straight to the
        # resolution would spell the first as the second.
        anchor = (
            self._esde_system_dir(by_system, spec.system)
            if read
            else _RomDirectory(caveats=_catalogue_unread_caveat(spec.system))
        )
        override_label = (
            None
            if anchor.directory is None
            else _match_per_game(selections, content_path, system_roms_dir=anchor.directory)
        )
        if override_label is None or override_label == spec.label:
            return (*status, *anchor.caveats)
        return (*status, *anchor.caveats, _per_game_override_caveat(override_label, spec))

    def entry_savefile_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> SavefilePlacement | Unresolved:
        """The entry route behind :meth:`EmulatorEntry.savefile_location` — one marker read.

        Resolves the placement for a catalogue entry and, when *content_path*
        is given, checks the gamelist for a per-game override that would launch
        a different emulator — all from one snapshot of the governing sources.
        """
        config, marker_issues = self._read_marker()
        extra = (
            self._entry_caveats_for(config, spec, content_path)
            if content_path is not None
            else ()
        )
        if spec.kind != KIND_LIBRETRO:
            card = lookup_standalone_save_card(emulator_token(spec.command))
            if card is None or spec.system not in card.systems:
                return _standalone_savefile_unresolved(spec)
            health = self._health_from(config, marker_issues)
            return _standalone_savefile_placement(
                self._machine,
                card=card,
                homes=self._xdg_homes(),
                sandbox=self._sandbox(),
                system=spec.system,
                command=spec.command,
                extra_caveats=(
                    *entry_caveats,
                    *extra,
                    *health.issues,
                    *arrangement_caveats(self.kind, observed_version=_marker_version(config)),
                ),
                content_path=content_path,
            )
        placement = self._savefile_location_from(
            config,
            marker_issues,
            content_path=content_path,
            core_so=spec.core_so,
            system=spec.system,
            extra_caveats=entry_caveats,
        )
        return _entry_savefile_with_caveats(placement, extra)

    def entry_savestate_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> SavestatePlacement | SavestateAbsence | Unresolved:
        """The entry route behind :meth:`EmulatorEntry.savestate_location` — one marker read.

        The savefile route's twin, down to the per-game override check: which
        emulator ES-DE would actually launch decides both answers, so the one
        that would not launch says so on both. A standalone entry goes through
        the savestate card the same way a save goes through its own card, and
        refuses identically where none covers it (#225). An absence card
        answers the stated no on this route, before any tree is touched
        (#284) — the dispatch refuses to handle one, because only the route
        holds the caveats that qualify the claim: the catalogue-status and
        per-game-override caveats the save twin carries, and the
        arrangement's evidence caveats.
        """
        config, marker_issues = self._read_marker()
        extra = (
            self._entry_caveats_for(config, spec, content_path)
            if content_path is not None
            else ()
        )
        if spec.kind != KIND_LIBRETRO:
            card = lookup_standalone_savestate_card(emulator_token(spec.command))
            if card is None or spec.system not in card.systems:
                return _standalone_savestate_unresolved(spec)
            if card.absent is not None:
                # The stated no reads no tree, so no homes are built for it —
                # but everything qualifying the CLAIM rides: which emulator
                # the gamelist would really launch, and the arrangement's own
                # evidence, exactly as on every placement.
                return _savestate_absence_answer(
                    card,
                    entry=(*entry_caveats, *extra),
                    arrangement=arrangement_caveats(
                        self.kind, observed_version=_marker_version(config)
                    ),
                )
            health = self._health_from(config, marker_issues)
            return _standalone_savestate_placement(
                self._machine,
                card=card,
                homes=self._xdg_homes(),
                sandbox=self._sandbox(),
                system=spec.system,
                command=spec.command,
                extra_caveats=(
                    *entry_caveats,
                    *extra,
                    *health.issues,
                    *arrangement_caveats(self.kind, observed_version=_marker_version(config)),
                ),
                content_path=content_path,
            )
        placement = self._savestate_location_from(
            config,
            marker_issues,
            content_path=content_path,
            core_so=spec.core_so,
            extra_caveats=entry_caveats,
        )
        return _entry_savestate_with_caveats(placement, extra)

    def _xdg_homes(self) -> _XdgHomes:
        """Where the emulators RetroDECK ships keep their own XDG trees.

        One flatpak holds all of them, so one pair of bases serves every
        standalone row — and flatpak pins those bases after applying every
        override, which is what makes a standalone texture directory a path
        join rather than a config read.
        """
        app_dir = os.path.join(self._home, ".var", "app", self._APP_ID)
        return _XdgHomes(
            data=os.path.join(app_dir, "data"),
            config=os.path.join(app_dir, "config"),
            # RetroDECK's emulators are its own bundled builds — no app id of
            # their own — and every one of them runs inside this flatpak, so
            # the XDG variables they read are the pinned ones.
            xdg_pinned=True,
        )

    def entry_texture_pack_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> TexturePlacement | Unresolved:
        """The entry route behind :meth:`EmulatorEntry.texture_pack_location` — one marker read.

        Both kinds of entry answer here, which is what the save routes cannot
        do: a libretro entry goes through the core route, and a standalone one
        through its own XDG join, refusing only where no packaged card covers
        the emulator the command names.

        The per-game override check rides along for the reason it rides the two
        save routes: an entry ES-DE would not launch for this game reads no
        texture packs for it either.
        """
        config, marker_issues = self._read_marker()
        extra = (
            self._entry_caveats_for(config, spec, content_path)
            if content_path is not None
            else ()
        )
        if spec.kind != KIND_LIBRETRO:
            card = lookup_standalone_texture_card(emulator_token(spec.command))
            if card is None:
                return _standalone_texture_unresolved(spec)
            health = self._health_from(config, marker_issues)
            return _standalone_texture_placement(
                self._machine,
                card=card,
                homes=self._xdg_homes(),
                sandbox=self._sandbox(),
                extra_caveats=(
                    *entry_caveats,
                    *extra,
                    *health.issues,
                    *arrangement_caveats(self.kind, observed_version=_marker_version(config)),
                ),
            )
        placement = self._texture_pack_location_from(
            config,
            marker_issues,
            content_path=content_path,
            core_so=spec.core_so,
            extra_caveats=entry_caveats,
        )
        return _entry_texture_with_caveats(placement, extra)

    def entry_mod_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> ModPlacement | Unresolved:
        """The entry route behind :meth:`EmulatorEntry.mod_location` — one marker read.

        The texture entry route's twin, down to the per-game override check: an
        entry ES-DE would not launch for this game reads no mods for it either.
        """
        config, marker_issues = self._read_marker()
        extra = (
            self._entry_caveats_for(config, spec, content_path)
            if content_path is not None
            else ()
        )
        if spec.kind != KIND_LIBRETRO:
            card = lookup_standalone_mod_card(emulator_token(spec.command))
            if card is None:
                return _standalone_mod_unresolved(spec)
            health = self._health_from(config, marker_issues)
            return _standalone_mod_placement(
                self._machine,
                card=card,
                homes=self._xdg_homes(),
                sandbox=self._sandbox(),
                extra_caveats=(
                    *entry_caveats,
                    *extra,
                    *health.issues,
                    *arrangement_caveats(self.kind, observed_version=_marker_version(config)),
                ),
            )
        placement = self._mod_location_from(
            config,
            marker_issues,
            content_path=content_path,
            core_so=spec.core_so,
            extra_caveats=entry_caveats,
        )
        return _entry_mod_with_caveats(placement, extra)


def _dequote_shell_value(value: str) -> str:
    """Bash's quote removal over *value*'s segments — concatenation included.

    EmuDeck composes values out of quoted and bare segments: the app-driven
    installer reads ``storagePath`` with ``jq`` and no ``-r``, so the JSON
    quotes land inside the value and every path key is written as
    ``romsPath="/run/media/deck/Emulation"/Emulation/roms``
    (``jsonToBashVars.sh:116-123``, upstream ``dragoonDorise/EmuDeck`` @
    ``863ab69`` — observed on a live installation). ``source`` reads that as
    one word: a quoted segment contributes its contents, a bare segment
    itself. Stripping only a whole-value wrap kept the embedded quotes, and
    every marker-derived root became a path that exists nowhere — health then
    false-alarmed ``root-missing`` on a healthy machine.

    Quote removal and nothing more: an unterminated quote returns the value
    verbatim (bash refuses such a line; atlas does not emulate a shell), and
    a backslash is an ordinary character — bash's own reading inside single
    quotes and for every non-special character inside double quotes, and the
    observed marker corpus carries no escape sequences at all (91 bare
    values, 26 whole-quoted, 7 partially-quoted path keys).
    """
    parts: list[str] = []
    i = 0
    while i < len(value):
        quote = value[i]
        if quote in "\"'":
            end = value.find(quote, i + 1)
            if end == -1:
                return value
            parts.append(value[i + 1 : end])
            i = end + 1
        else:
            cuts = [p for p in (value.find('"', i), value.find("'", i)) if p != -1]
            end = min(cuts) if cuts else len(value)
            parts.append(value[i:end])
            i = end
    return "".join(parts)


def _parse_settings_sh(text: str, *, home: str) -> dict[str, str]:
    """Parse EmuDeck's ``settings.sh`` (``key=value`` shell lines) into a dict.

    Values go through bash's quote removal (:func:`_dequote_shell_value`) and
    literal ``$HOME`` / ``${HOME}`` is expanded against the machine home — the
    forms EmuDeck actually writes. Anything fancier stays verbatim; atlas does
    not emulate a shell.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _dequote_shell_value(value.strip())
        value = value.replace("${HOME}", home).replace("$HOME", home)
        if key:
            result[key] = value
    return result


# EmuDeck's settings.sh records which frontends its installer was told to set
# up — ``jsonToBashVars.sh:71`` writes ``doInstallESDE`` as a plain key=value
# line (upstream ``dragoonDorise/EmuDeck`` @ ``863ab69``). The disk decides
# whether ES-DE is answered from — EmuDeck's own presence test is the AppImage
# stat (``ESDE_IsInstalled``, ``emuDeckESDE.sh:488-494``) — and when the
# record and the disk disagree, the answer states the disagreement instead of
# silently following either side.
CAVEAT_FRONTEND_MARKER_MISMATCH = "frontend-marker-mismatch"


@dataclass(frozen=True, slots=True)
class _EsdeSnapshot:
    """One catalogue query's reading of EmuDeck's ES-DE side.

    ``refusal`` set means the answer is over: it is the complete caveat list
    of an answer that enumerates nothing — no ES-DE on disk, or a broken
    resource-override shadow — findings, reason and cross-check already in
    answer order. ``refusal`` ``None`` means the layers were read:
    ``by_system`` holds the enumeration, ``complete`` whether it is the whole
    catalogue in force (the shadow stood in for the bundled layer, or the
    custom layer excluded it — ``exclusive`` says which), ``relocated``
    whether a ``portable.txt`` casts doubt on the reads, and ``tail`` the
    caveats every enumerating answer states after its findings (the
    catalogue-status statement — exclusive or sealed — then relocation, then
    the marker cross-check: the pinned order).
    """

    findings: tuple[Caveat, ...]
    refusal: tuple[Caveat, ...] | None
    by_system: Mapping[str, SystemDeclaration]
    complete: bool
    relocated: bool
    tail: tuple[Caveat, ...]
    exclusive: bool = False
    # The companion cfg's text, from the same single read whose status decided
    # the companion health finding — carried so the derived-enumeration branch
    # (issue #133) can resolve the cores without opening the file a second
    # time inside one query.
    companion_text: str | None = None


class EmuDeck(_FirmwareQueries, _CatalogueQueries):
    """An EmuDeck arrangement — ``settings.sh`` is its truth, the bare
    ``org.libretro.RetroArch`` Flatpak is its RetroArch.

    The handle carries both descriptions (``kinds``): EmuDeck *is* a configured
    bare RetroArch, so both statements are true of the same installation.
    Like every handle it is live — one snapshot of each source per query
    (REVIEW M4) — and its health covers the claimed companion RetroArch config,
    so a stale ``settings.sh`` next to a vanished Flatpak is visible instead of
    silently suppressing the bare handle (REVIEW H10).

    Its frontend side: EmuDeck deploys **upstream ES-DE as an AppImage** at
    ``~/Applications/ES-DE.AppImage`` (``vars.sh:4-6``, ``emuDeckESDE.sh:9-10``,
    upstream ``dragoonDorise/EmuDeck`` @ ``863ab69``; the stable
    ``LinuxSteamDeckAppImage`` package from ES-DE's own ``latest_release.json``,
    ``emuDeckESDE.sh:15,91-98``) and wires it through plain files under
    ``~/ES-DE`` — the application data directory ES-DE resolves when launched
    with no ``--home`` and no ``ESDE_APPDATA_DIR``, which is how EmuDeck's
    launcher runs it (``tools/launchers/es-de/es-de.sh:5``; ES-DE v3.4.1
    ``FileSystemUtil.cpp:259-285``). The catalogue answers read those files;
    what stays out of reach is the **bundled** ``es_systems.xml``, which the
    AppImage embeds (ES-DE ``INSTALL.md`` v3.4.1:1470) — see
    :meth:`_read_esde_catalogue`.
    """

    kind = "emudeck"
    kinds = ("emudeck", "bare_retroarch_flatpak")
    _RA_APP_ID = RETROARCH_FLATPAK_APP_ID

    def _catalogue_absence(self) -> Caveat:
        # Not "there is none": this EmuDeck runs no ES-DE atlas can find, and
        # what its frontend is instead — Pegasus, Steam ROM Manager — is what
        # atlas has not established. The honest answer is about atlas, so the
        # code says unestablished and the message does not claim an absence.
        return Caveat(
            CAVEAT_EMULATOR_CATALOGUE_UNESTABLISHED,
            "no ES-DE is present on this EmuDeck arrangement (no ~/Applications/ES-DE.AppImage, no "
            "~/ES-DE tree) — EmuDeck can also be driven by Pegasus or Steam ROM Manager, and atlas "
            "has not established where those keep a catalogue, so which emulators run a system is "
            "unknown here; name the core yourself with savefile_location(core_so=...)",
            {"arrangement": "emudeck"},
        )

    def __init__(self, home: str, machine: Machine) -> None:
        self._home = home
        self._machine = machine

    def _marker_path(self) -> str:
        return os.path.join(self._home, EMUDECK_SETTINGS_SUFFIX)

    def _read_marker(self) -> tuple[dict[str, str], tuple[Caveat, ...]]:
        """One live read of ``settings.sh`` → (settings, marker issues)."""
        path = self._marker_path()
        result = self._machine.read_text(path)
        if result.status == READ_MISSING:
            return {}, (Caveat(HEALTH_ISSUE_MARKER_MISSING, f"marker {path} does not exist", {"path": path}),)
        if result.text is None:
            return {}, (
                Caveat(
                    HEALTH_ISSUE_MARKER_UNREADABLE,
                    f"marker {path} cannot be read as text ({result.status})",
                    {"path": path, "status": result.status},
                ),
            )
        return _parse_settings_sh(result.text, home=self._home), ()

    # EmuDeck's installer *is* a git checkout — ``install.sh`` clones the
    # backend to ``~/.config/EmuDeck/backend`` (upstream ``dragoonDorise/
    # EmuDeck`` @ ``863ab69``) — and that checkout's HEAD is the one version
    # statement the arrangement leaves on disk: ``settings.sh`` names none.
    _BACKEND_GIT_SUFFIX = os.path.join(_XDG_CONFIG_DIRNAME, "EmuDeck", "backend", ".git")

    def _observed_backend_head(self) -> str | None:
        """The backend commit this machine runs — EmuDeck's version statement.

        Read as the two plain files git keeps HEAD in, no git invocation:
        ``.git/HEAD`` either names the commit directly (detached) or carries a
        ``ref:`` line naming a ref whose loose file holds the hash — the shape
        a live installation was observed with (``ref: refs/heads/main``).
        Anything that stops the walk — a missing or unreadable file, an empty
        ref, a ref packed away where no loose file exists — answers ``None``:
        the machine states no version, and per
        :func:`atlas.evidence.arrangement_caveats` that silence means *no
        drift established*, never "no drift" — the same state an unreadable
        ``retrodeck.json`` version leaves RetroDECK in. Following packed refs
        is a possible refinement; the two plain files are what is read today.
        """
        git_dir = os.path.join(self._home, self._BACKEND_GIT_SUFFIX)
        head = self._machine.read_text(os.path.join(git_dir, "HEAD")).text
        if head is None:
            return None
        head = head.strip()
        if head.startswith("ref:"):
            ref = head[len("ref:") :].strip()
            if not ref:
                return None
            resolved = self._machine.read_text(os.path.join(git_dir, ref)).text
            if resolved is None:
                return None
            return resolved.strip() or None
        return head or None

    def _setting_path(self, settings: dict[str, str], key: str, fallback_subdir: str) -> tuple[str, str]:
        value = settings.get(key, "")
        if value:
            return value, f"settings.sh: {key}"
        fallback = os.path.join(self._home, "Emulation", fallback_subdir)
        return fallback, f"default: {key} unset → {fallback} (EmuDeck default)"

    def root(self) -> str:
        """The EmuDeck ``Emulation`` tree root (parent of ``romsPath``)."""
        return os.path.dirname(self._setting_path(self._read_marker()[0], "romsPath", "roms")[0])

    def saves_root(self) -> str:
        """EmuDeck's saves root (``savesPath`` or the default)."""
        return self._setting_path(self._read_marker()[0], "savesPath", "saves")[0]

    def bios_dir(self) -> str:
        """EmuDeck's BIOS directory (``biosPath`` or the default)."""
        return self._setting_path(self._read_marker()[0], "biosPath", "bios")[0]

    def roms_dir(self) -> str | None:
        """The ROM root the frontend substitutes for ``%ROMPATH%`` — or ``None``.

        The **root**, not a system's directory: a system's own directory is
        ``rom_location(system).dir``, which is this plus whatever ``<path>`` the
        catalogue declares for it — usually the system name, and not reliably
        so, which is why the two are different questions.

        Read from ES-DE's ``ROMDirectory`` rather than ``settings.sh``'s
        ``romsPath``. The two agree on a stock installation and are wired one
        way — EmuDeck seds its own value into ES-DE's setting
        (``ESDE_setDefaultSettings``, ``emuDeckESDE.sh:407-409``) and never
        reads it back — so where a user has moved them apart, only this one is
        what the frontend launches from. The same cfg-over-marker rule as
        RetroDECK's :meth:`RetroDeck.roms_dir`, over this arrangement's files.

        ``None`` is a refusal, never "no ROMs here", and it is the same refusal
        :meth:`rom_location` states — for the reasons that belong to the root:
        no ES-DE is on this disk at all (so there is no frontend whose
        substitution this could be), ES-DE's settings exist and could not be
        read, a ``portable.txt`` may have moved the tree the frontend's own
        home-derived answers come from — the unset default and a ``~`` in the
        setting alike — or the configured value is not an absolute path even
        after the frontend's own ``~`` expansion. A bare string cannot carry
        which, and raising is not this domain's grammar, so a caller who needs
        the reason asks ``rom_location(system)`` and reads its caveats.
        """
        if not self._esde_present():
            return None
        return self._esde_rom_root(relocated=self._relocation_caveat() is not None).directory

    def _companion_cfg_path(self) -> str:
        return os.path.join(self._home, STANDALONE_FLATPAK_CFG_SUFFIX)

    def _health_from(
        self,
        settings: dict[str, str],
        marker_issues: tuple[Caveat, ...],
        companion_status: ReadStatus,
    ) -> Health:
        issues = list(marker_issues)
        root = os.path.dirname(self._setting_path(settings, "romsPath", "roms")[0])
        if self._machine.path_kind(root) != KIND_DIRECTORY:
            issues.append(
                Caveat(HEALTH_ISSUE_ROOT_MISSING, f"root {root} is not an existing directory", {"path": root})
            )
        saves = self._setting_path(settings, "savesPath", "saves")[0]
        if self._machine.path_kind(saves) != KIND_DIRECTORY:
            issues.append(
                Caveat(
                    HEALTH_ISSUE_SAVES_ROOT_MISSING,
                    f"saves root {saves} is not an existing directory",
                    {"path": saves},
                )
            )
        if companion_status != "ok":
            path = self._companion_cfg_path()
            issues.append(
                Caveat(
                    HEALTH_ISSUE_COMPANION_CONFIG_MISSING,
                    f"the claimed org.libretro.RetroArch config {path} is not readable "
                    f"({companion_status}) — the arrangement's RetroArch side is broken or stale",
                    {"path": path, "status": companion_status},
                )
            )
        return Health(tuple(issues))

    def health(self) -> Health:
        """Installation health — marker, roots, companion RetroArch config, catalogue loadable.

        The catalogue check lives here rather than in :meth:`_health_from`
        for RetroDECK's reason: per-question health must not open sources the
        question never reads, so the finding rides this question's own single
        read and every catalogue-reading answer — never the others.
        """
        settings, marker_issues = self._read_marker()
        companion_status = self._machine.read_text(self._companion_cfg_path()).status
        health = self._health_from(settings, marker_issues, companion_status)
        catalogue_invalid = self._read_esde_catalogue()[4]
        if catalogue_invalid is not None:
            return Health((*health.issues, catalogue_invalid))
        return health

    # ── EmuDeck's ES-DE ─────────────────────────────────────────────────
    # Every path below is EmuDeck's shipped wiring, read from the installer at
    # the pinned revision (dragoonDorise/EmuDeck @ 863ab69) and from ES-DE's
    # own source at v3.4.1 — docs/research/retrodeck-save-placement.md §13
    # holds the evidence.

    _ESDE_APPIMAGE_SUFFIX = os.path.join("Applications", "ES-DE.AppImage")
    # The embedded catalogue's path inside the AppImage's squashfs: the file
    # the Linux build loads (SystemData.cpp:1349-1351 @ v3.4.1), at the prefix
    # the AppImage packs it under — verified against the deployed image.
    _ESDE_APPIMAGE_CATALOGUE_ENTRY = "usr/share/es-de/resources/systems/linux/es_systems.xml"
    _ESDE_PORTABLE_SUFFIX = os.path.join("Applications", "portable.txt")
    _ESDE_APPDATA_DIRNAME = "ES-DE"
    _ESDE_SHADOW_SUFFIX = os.path.join("resources", "systems", "linux", _ES_SYSTEMS_XML)
    _ESDE_OVERLAY_SUFFIX = os.path.join("custom_systems", _ES_SYSTEMS_XML)
    _ESDE_SETTINGS_SUFFIX = os.path.join("settings", "es_settings.xml")
    _ROM_DIRECTORY_SETTING = "ROMDirectory"
    _FRONTEND_MARKER_KEY = "doInstallESDE"
    _CATALOGUE_SOURCE = (
        "ES-DE catalogue read live (es_systems.xml — on-disk layers under ~/ES-DE, "
        "and the AppImage-embedded bundled layer where the runtime can open it)"
    )
    _ROM_DIRECTORY_SOURCE = "ES-DE ROMDirectory read live (es_settings.xml)"

    def _esde_appdata_dir(self) -> str:
        """ES-DE's application data directory — plain ``~/ES-DE``.

        EmuDeck's launcher runs the AppImage with no ``--home`` and no
        ``ESDE_APPDATA_DIR`` (``tools/launchers/es-de/es-de.sh:5``), so the
        upstream resolution lands on ``<home>/ES-DE`` (ES-DE v3.4.1
        ``FileSystemUtil.cpp:259-285``). A user-set ``ESDE_APPDATA_DIR`` in the
        launch environment would move it and is written nowhere on disk — that
        residual is documented, not probed; the on-disk relocation
        (``portable.txt``) is (:meth:`_relocation_caveat`).
        """
        return os.path.join(self._home, self._ESDE_APPDATA_DIRNAME)

    def _esde_appimage_path(self) -> str:
        return os.path.join(self._home, self._ESDE_APPIMAGE_SUFFIX)

    def _esde_present(self) -> bool:
        """Whether ES-DE is on this disk — the AppImage, or its appdata tree.

        The AppImage stat is EmuDeck's own installed-test
        (``ESDE_IsInstalled``, ``emuDeckESDE.sh:488-494``: ``[ -e
        "$ESDE_toolPath" ]``); the ``~/ES-DE`` tree covers an AppImage moved
        or renamed out from under a configuration that still runs. Disk
        decides — ``settings.sh``'s own record is a cross-check
        (:meth:`_frontend_marker_caveat`), never the decision.
        """
        if self._machine.path_kind(self._esde_appimage_path()) != KIND_MISSING:
            return True
        return self._machine.path_kind(self._esde_appdata_dir()) == KIND_DIRECTORY

    def _relocation_caveat(self) -> Caveat | None:
        """The ``portable.txt`` statement, when one sits next to the AppImage — else ``None``.

        ES-DE reads ``portable.txt`` from its executable directory and moves
        its home by it — but only after validating the target: a path that
        does not exist or is a regular file is rejected and the default home
        stays (ES-DE v3.4.1 ``main.cpp:149-192``; the validation block is
        ``:174-192``). EmuDeck writes none; a user can. Atlas stats only the
        file's presence, so the honest claim is *may*: when one is there,
        every ``~/ES-DE`` read this handle makes may be reading files the
        frontend is not using — the answers still state what the on-disk tree
        says, and this caveat states the suspicion, never silently. Reading
        the file and validating its target the way ES-DE does is a possible
        refinement; presence alone is what is stated today.
        """
        path = os.path.join(self._home, self._ESDE_PORTABLE_SUFFIX)
        if self._machine.path_kind(path) == KIND_MISSING:
            return None
        return Caveat(
            CAVEAT_CONFIG_HOME_RELOCATED,
            f"a portable.txt sits next to the ES-DE AppImage at {path}, which may relocate "
            "ES-DE's application data directory away from ~/ES-DE (ES-DE ignores it when its "
            "target is missing or a file) — the on-disk files this answer was read from may "
            "not be the ones the frontend is using",
            {"path": path},
        )

    def _frontend_marker_caveat(self, settings: dict[str, str], present: bool) -> Caveat | None:
        """The cross-check: ``settings.sh``'s ES-DE record against the disk — ``None`` in agreement.

        ``doInstallESDE`` records the installer's choice
        (``jsonToBashVars.sh:71``); the disk records what is actually here.
        The disk decides the answer either way — this caveat exists so a stale
        record is stated rather than discovered. A marker that writes neither
        ``true`` nor ``false`` states nothing, and no disagreement can be
        manufactured from silence.
        """
        stated = settings.get(self._FRONTEND_MARKER_KEY)
        if stated not in ("true", "false"):
            return None
        if (stated == "true") == present:
            return None
        observed = "present" if present else "absent"
        consequence = (
            "the answer is read from the ES-DE on disk"
            if present
            else "no ES-DE answers here"
        )
        return Caveat(
            CAVEAT_FRONTEND_MARKER_MISMATCH,
            f"settings.sh states {self._FRONTEND_MARKER_KEY}={stated} while ES-DE is {observed} "
            f"on disk — the marker's record is stale or the frontend changed hands; {consequence}, "
            "because the disk is what runs",
            {"key": self._FRONTEND_MARKER_KEY, "stated": stated, "observed": observed},
        )

    def _riders(self, settings: dict[str, str], present: bool) -> tuple[Caveat, ...]:
        """The statements that ride beside the catalogue status, in the pinned order.

        The relocation suspicion first, then the marker cross-check — one
        builder, consumed by :meth:`_esde_snapshot`'s tail and by the firmware
        route, so the two can never spell the order apart. The relocation read
        happens only while an ES-DE is present: with none on disk there is
        nothing a ``portable.txt`` could move out from under.
        """
        cross_check = self._frontend_marker_caveat(settings, present)
        mismatch = () if cross_check is None else (cross_check,)
        if not present:
            return mismatch
        portable = self._relocation_caveat()
        relocation = () if portable is None else (portable,)
        return (*relocation, *mismatch)

    def _catalogue_sealed_caveat(self, system: str | None = None) -> Caveat:
        # Reading the sealed layer itself (extracting the AppImage's squashfs)
        # is tracked as issue #65; until then the on-disk layers are the whole
        # readable catalogue and every catalogue answer says so.
        return Caveat(
            CAVEAT_EMULATOR_CATALOGUE_SEALED,
            "ES-DE's bundled es_systems.xml is sealed inside the AppImage, which atlas does not "
            "open — only the on-disk layers under ~/ES-DE were read, so this enumeration is "
            "incomplete: a system or emulator this answer does not name may still be declared by "
            "the frontend",
            {"system": system} if system is not None else {},
        )

    def _catalogue_exclusive(self, system: str | None = None) -> tuple[Caveat, ...]:
        return _catalogue_exclusive_caveat(
            os.path.join(self._esde_appdata_dir(), self._ESDE_OVERLAY_SUFFIX), system
        )

    def _read_esde_catalogue(
        self,
    ) -> tuple[dict[str, SystemDeclaration], bool, bool, bool, Caveat | None]:
        """The readable ES-DE layers → ``(by_system, complete, shadow_broken, exclusive, invalid)``.

        The overlay is read first because it can end the reading: a
        document-level ``<loadExclusive/>`` in the custom file makes ES-DE
        skip the bundled layer wholesale — embedded *and* resource-override
        shadow alike, since the skipped path is whatever ``getResourcePath``
        resolved (``SystemData.cpp:858-895,1338-1362`` @ v3.4.1). The
        catalogue is then the overlay alone and **complete**: nothing sealed
        applies, and a broken shadow cannot matter on such a machine because
        the frontend never opens it either.

        On the merged path the bundled layer has two on-machine sources, in
        ES-DE's own precedence. First the per-file resource override
        (``INSTALL.md`` v3.4.1:1125): a file at
        ``~/ES-DE/resources/systems/linux/es_systems.xml`` shadows the
        embedded one for ES-DE itself, so where it exists and parses it *is*
        the bundled layer, on disk — ``complete`` is ``True`` and nothing is
        sealed away. A shadow that exists and cannot be read or parsed is
        ``shadow_broken``: ES-DE loads that file, atlas could not, and what
        the catalogue says is then unknown — the same claim RetroDECK's
        unreadable bundled layer makes. Where no shadow exists, the embedded
        file itself is read **out of the AppImage** (issue #65): the image's
        squashfs is opened by :mod:`atlas.squashfs` at
        ``usr/share/es-de/resources/systems/linux/es_systems.xml`` — the
        path the Linux build loads (``SystemData.cpp:1349-1351``), verified
        against the deployed AppImage — and where that read answers text,
        the catalogue is ``complete`` and nothing is sealed. Every other
        outcome of that read — no AppImage, not an AppImage, no such entry,
        an interpreter without the image's codec (zstd needs Python >= 3.14),
        unreadable bytes — leaves the ``sealed`` state exactly as it always
        was: ``complete`` ``False``, the caveat naming the sealed layer. A
        ``<loadExclusive/>`` in the shadow or the embedded file is ignored
        the way ES-DE ignores one in the bundled layer (the LogWarning
        branch, ``:886-895``).

        The overlay is EmuDeck's own write (``emuDeckESDE.sh:18,127``,
        deployed from ``configs/emulationstation/custom_systems/`` and
        path-rewritten at ``:144-145``): unlike RetroDECK's commented-out
        stub, it declares real systems, and per ES-DE's merge semantics a
        system it declares is *exactly* the one the frontend uses — the
        sealed layer cannot contradict a same-name overlay system. EmuDeck
        never writes the tag; carrying one is a user's own edit, honored
        because the frontend honors it.

        ``invalid`` is the hardest state (issue #100), and it outranks
        ``shadow_broken``: a layer atlas **read** and ES-DE cannot load
        aborts the frontend's whole ``loadConfig``, so the catalogue in
        force is empty — with the health finding naming the file. A shadow
        atlas could not read stays ``shadow_broken``: ES-DE may load it
        fine, so what the catalogue says is unknown, never refused.
        """
        appdata = self._esde_appdata_dir()
        custom = CatalogueLayer(systems={})
        overlay_path = os.path.join(appdata, self._ESDE_OVERLAY_SUFFIX)
        custom_text = self._machine.read_text(overlay_path).text
        if custom_text is not None:
            custom = parse_es_systems(custom_text, provenance="es_systems.xml (custom_systems overlay)")
        if custom.invalid is not None:
            return {}, True, False, False, _catalogue_invalid_finding(overlay_path, custom.invalid)
        if custom.load_exclusive:
            return dict(custom.systems), True, False, True, None
        bundled, complete, shadow_broken, bundled_invalid = self._bundled_layer(appdata)
        if bundled_invalid is not None:
            return {}, True, False, False, bundled_invalid
        if shadow_broken:
            return {}, False, True, False, None
        return merge_layers(bundled.systems, custom.systems), complete, False, False, None

    def _bundled_layer(self, appdata: str) -> tuple[CatalogueLayer, bool, bool, Caveat | None]:
        """The bundled layer's one reading → ``(layer, complete, shadow_broken, invalid)``.

        ES-DE's own precedence: the on-disk resource-override shadow outranks
        the AppImage-embedded file, so the embedded read runs only where no
        shadow exists at all. Read and parsed — from either source — the
        layer is the bundled one and ``complete`` is ``True``; a shadow that
        exists and yields no text is ``shadow_broken``; a layer that parses
        invalid is the finding; and every unreadable-image outcome is the
        sealed state the caller already spells: nothing read, nothing broken.
        """
        empty = CatalogueLayer(systems={})
        shadow_path = os.path.join(appdata, self._ESDE_SHADOW_SUFFIX)
        shadow = self._machine.read_text(shadow_path)
        if shadow.status != READ_MISSING:
            if shadow.text is None:
                return empty, False, True, None
            bundled = parse_es_systems(
                shadow.text, provenance="es_systems.xml (resource-override shadow)"
            )
            if bundled.invalid is not None:
                return empty, True, False, _catalogue_invalid_finding(shadow_path, bundled.invalid)
            # Read AND parsed: the shadow is the bundled layer, on disk.
            return bundled, True, False, None
        embedded, invalid = self._embedded_bundled_layer()
        if invalid is not None:
            return empty, True, False, invalid
        if embedded is not None:
            # Read AND parsed, straight out of the image: the bundled layer
            # itself, not a stand-in — nothing is sealed.
            return embedded, True, False, None
        return empty, False, False, None

    def _embedded_bundled_layer(self) -> tuple[CatalogueLayer | None, Caveat | None]:
        """The AppImage-embedded bundled layer → ``(layer, invalid finding)``.

        ``(None, None)`` is every outcome short of text — no AppImage, not an
        AppImage, no such entry, a runtime without the image's codec,
        unreadable bytes — and leaves the caller's sealed state untouched. A
        layer that parses invalid is the finding instead: ES-DE loads the
        same embedded file and aborts its whole ``loadConfig`` on it.
        """
        appimage_path = self._esde_appimage_path()
        embedded = self._machine.read_appimage_text(
            appimage_path, self._ESDE_APPIMAGE_CATALOGUE_ENTRY
        )
        if embedded.text is None:
            return None, None
        bundled = parse_es_systems(embedded.text, provenance="es_systems.xml (AppImage-embedded)")
        if bundled.invalid is not None:
            return None, _catalogue_invalid_finding(appimage_path, bundled.invalid)
        return bundled, None

    def _gamelist_selections(self, system: str) -> GamelistSelections:
        """The gamelist's emulator selections — EmuDeck seeds these itself.

        ``~/ES-DE/gamelists/<system>/gamelist.xml`` is upstream's location
        (ES-DE ``INSTALL.md`` v3.4.1:2145), and EmuDeck writes per-system
        ``<alternativeEmulator>`` defaults into it (``ESDE_setEmu``,
        ``emuDeckESDE.sh:427-480``) — the standalone-heavy selections are the
        arrangement's normal state, not a user customization.
        """
        path = os.path.join(self._esde_appdata_dir(), "gamelists", system, "gamelist.xml")
        text = self._machine.read_text(path).text
        if text is None:
            return GamelistSelections(system_label=None, per_game={})
        return parse_gamelist(text)

    def _esde_settings_path(self) -> str:
        return os.path.join(self._esde_appdata_dir(), self._ESDE_SETTINGS_SUFFIX)

    def _rom_directory(self) -> tuple[str | None, str | None]:
        """The configured ``ROMDirectory``, and the status that stopped the reading.

        The same three-outcome rule as RetroDECK's reading of the same file
        (one grammar, one settings format): missing or set-nothing answers
        ``(None, None)`` and the frontend's default applies; a file that is
        there and could not be read or parsed answers the status, and no
        directory. EmuDeck writes the value itself — ``ESDE_setDefaultSettings``
        seds ``${romsPath}`` into it (``emuDeckESDE.sh:407-409``) — so on a
        stock machine it is configured and absolute.
        """
        result = self._machine.read_text(self._esde_settings_path())
        if result.status == READ_MISSING:
            return None, None
        if result.text is None:
            return None, result.status
        settings = parse_es_settings(result.text)
        if settings is None:
            return None, _SETTINGS_UNPARSEABLE
        return settings.get(self._ROM_DIRECTORY_SETTING) or None, None

    def _esde_rom_root(self, *, relocated: bool) -> _RomRoot:
        """What this ES-DE substitutes for ``%ROMPATH%`` — or which way it could not be established.

        The unset default is ``~/ROMs``: ES-DE falls back on ``<home>/ROMs``
        (``FileData.cpp::getROMDirectory()``, ES-DE v3.4.1, ``:271-305``, the
        empty-setting branch at ``:283-284``), and this ES-DE's home is the
        user's own — the launcher passes no ``--home`` (``es-de.sh:5``). A
        configured value carrying ``~`` expands against that same home, as
        text (:func:`atlas.esde.expand_home_path`; the call is
        ``FileData.cpp:289``). Both derivations are exactly what a
        ``portable.txt`` may move, so with one present both home-derived
        branches stop resolving (*relocated*) — a configured absolute value
        is still answered, with the answer-level relocation caveat riding.
        The absoluteness check runs on the *expanded* value; the raw one is
        what a refusal names, because the setting's own text is what a user
        edits.
        """
        configured, unreadable = self._rom_directory()
        if unreadable is not None:
            return _RomRoot(unreadable=unreadable)
        sources = (self._ROM_DIRECTORY_SOURCE,)
        portable = (os.path.join(self._home, self._ESDE_PORTABLE_SUFFIX), "portable.txt")
        if configured is None:
            if relocated:
                return _RomRoot(relocated=portable, sources=sources)
            return _RomRoot(directory=os.path.join(self._home, "ROMs"), sources=sources)
        expanded = configured
        if "~" in configured:
            if relocated:
                return _RomRoot(relocated=portable, sources=sources)
            expanded = expand_home_path(configured, self._home)
        if not expanded.startswith("/"):
            return _RomRoot(not_absolute=configured, sources=sources)
        return _RomRoot(directory=expanded, sources=sources)

    def _esde_system_dir(
        self,
        by_system: Mapping[str, SystemDeclaration],
        system: str,
        *,
        complete: bool,
        relocated: bool,
    ) -> _RomDirectory:
        """Where this ES-DE puts *system*'s ROMs — the root with the declared ``<path>`` applied.

        The same chain as RetroDECK's, with one branch that is EmuDeck's own:
        a system the readable layers do not declare is ``rom-path-undeclared``
        only when the enumeration is complete (the on-disk shadow stood in
        for the bundled layer, or the custom layer excluded it) — in the
        sealed state the declaration may sit in the layer nobody could read,
        and the caller's answer-level sealed caveat is that statement, so this
        branch adds nothing on top of it. The relocated branch is silent here
        for the same reason: the answer-level ``config-home-relocated`` caveat
        is the stated reason.
        """
        declaration = by_system.get(system)
        if declaration is None or declaration.rom_path is None:
            if complete:
                return _RomDirectory(caveats=_rom_path_undeclared_caveat(system, declaration))
            return _RomDirectory()
        declared = declaration.rom_path
        root = self._esde_rom_root(relocated=relocated)
        if root.unreadable is not None:
            return _RomDirectory(
                caveats=_settings_unreadable_caveat(system, self._esde_settings_path(), root.unreadable)
            )
        if root.relocated is not None:
            return _RomDirectory(sources=root.sources)
        if root.not_absolute is not None:
            return _RomDirectory(
                sources=root.sources,
                caveats=_rom_path_unresolved_caveat(system, declared, root.not_absolute),
            )
        resolved = resolve_rom_path(declared, root.directory)
        if resolved is None:
            # Unreachable as the branches above stand — same guard, same
            # reason as RetroDECK's: a directory that silently became None is
            # the one answer this question must never give.
            return _RomDirectory(
                sources=root.sources,
                caveats=_rom_path_unresolved_caveat(system, declared, root.directory or ""),
            )
        return _RomDirectory(directory=resolved, sources=root.sources)

    def _esde_snapshot(self, system: str | None = None) -> _EsdeSnapshot:
        """One read of every ES-DE-side source, in the pinned caveat order.

        The findings an answer states, the presence it decided and the layers
        it enumerates come from one revision of each file — a second read for
        any of them could see a different machine than the answer did (REVIEW
        M4). The caveat order every catalogue-shaped answer states is pinned
        here and only here: health findings lead (they qualify the
        installation), then the catalogue-status statement (exclusive,
        sealed, unreadable, or the unestablished refusal), then the riding statements
        in :meth:`_riders`'s one order (the relocation suspicion, then the
        marker cross-check), then whatever the per-question resolution has to
        say — and the evidence caveat the template method appends closes the
        list.
        """
        settings, marker_issues = self._read_marker()
        companion = self._machine.read_text(self._companion_cfg_path())
        findings = self._health_from(settings, marker_issues, companion.status).issues
        present = self._esde_present()
        riders = self._riders(settings, present)
        if not present:
            return _EsdeSnapshot(
                findings,
                (*findings, self._catalogue_absence(), *riders),
                {},
                False,
                False,
                (),
                companion_text=companion.text,
            )
        relocated = any(caveat.code == CAVEAT_CONFIG_HOME_RELOCATED for caveat in riders)
        by_system, complete, shadow_broken, exclusive, catalogue_invalid = (
            self._read_esde_catalogue()
        )
        if shadow_broken:
            return _EsdeSnapshot(
                findings,
                (*findings, *_catalogue_unread_caveat(system), *riders),
                {},
                False,
                relocated,
                (),
                companion_text=companion.text,
            )
        if exclusive:
            status: tuple[Caveat, ...] = self._catalogue_exclusive(system)
        else:
            status = () if complete else (self._catalogue_sealed_caveat(system),)
        if catalogue_invalid is not None:
            status = (catalogue_invalid, *status)
        return _EsdeSnapshot(
            findings,
            None,
            by_system,
            complete,
            relocated,
            (*status, *riders),
            exclusive,
            companion_text=companion.text,
        )

    def _systems_answer(self) -> tuple[SystemsAnswer, str | None]:
        """Every system the readable layers declare — joined by the derived ones while sealed.

        With the bundled layer sealed, the overlay's few systems are not the
        machine's whole answer: the installed cores' own declarations file
        under systems the readable layers never mention, and those join the
        list marked ``emulator-list-derived`` (issue #133). A complete
        catalogue (a readable shadow, or an exclusive overlay) answers alone —
        the derivation never overrides a read. The version travelling back is
        the backend checkout's HEAD (:meth:`_observed_backend_head`) —
        ``settings.sh`` names none, so the checkout is what this
        arrangement's evidence is weighed against. It rides the refusal too:
        which ES-DE is on disk and which backend this machine runs are
        separate facts.
        """
        version = self._observed_backend_head()
        snapshot = self._esde_snapshot()
        if snapshot.refusal is not None:
            return SystemsAnswer(caveats=snapshot.refusal), version
        systems = set(snapshot.by_system)
        derived_mark: tuple[Caveat, ...] = ()
        sources: tuple[str, ...] = (
            _CATALOGUE_SOURCE_EXCLUSIVE if snapshot.exclusive else self._CATALOGUE_SOURCE,
        )
        if not snapshot.complete:
            context = self._derived_context(snapshot)
            derived = {
                core.system for core in context.cores if core.system != "_unknown"
            } | {
                declaration.system
                for core in context.cores
                for declaration in core.firmware
                if declaration.system != "_unknown"
            }
            if derived - systems:
                systems |= derived
                sources = (*sources, *context.sources)
                derived_mark = (
                    Caveat(
                        CAVEAT_EMULATOR_LIST_DERIVED,
                        "the readable layers declare only part of the catalogue, so the systems "
                        "the installed cores' own declarations file under join this list — the "
                        "sealed bundled layer may declare a different set",
                        {},
                    ),
                )
        return (
            SystemsAnswer(
                tuple(sorted(systems)),
                sources,
                (*snapshot.findings, *snapshot.tail, *derived_mark),
            ),
            version,
        )

    def _platform_view(self) -> tuple[_PlatformView, str | None]:
        """EmuDeck's platform reading — overlay tags live, derived systems vocabulary-backed.

        Mirrors :meth:`_systems_answer` decision for decision: the readable
        layers' systems carry their own ``<platform>`` tags; while the bundled
        layer is sealed, the derived systems (issue #133) join with the
        snapshot column's tags, named as vocabulary-backed. The sealed file's
        text cannot be scanned, so no ``disabled`` map exists on this handle —
        a commented system degrades to *absent*, which is a true statement
        (nothing readable declares it), never a silently wrong one.
        """
        version = self._observed_backend_head()
        snapshot = self._esde_snapshot()
        if snapshot.refusal is not None:
            return _PlatformView({}, frozenset(), {}, (), snapshot.refusal), version
        declared = {
            name: declaration.platforms for name, declaration in snapshot.by_system.items()
        }
        vocabulary_backed: set[str] = set()
        sources: tuple[str, ...] = (
            _CATALOGUE_SOURCE_EXCLUSIVE if snapshot.exclusive else self._CATALOGUE_SOURCE,
        )
        derived_mark: tuple[Caveat, ...] = ()
        if not snapshot.complete:
            context = self._derived_context(snapshot)
            derived = {
                core.system for core in context.cores if core.system != "_unknown"
            } | {
                declaration.system
                for core in context.cores
                for declaration in core.firmware
                if declaration.system != "_unknown"
            }
            for system in derived - set(declared):
                declared[system] = vocabulary_platform_tags(system) or ()
                vocabulary_backed.add(system)
            if vocabulary_backed:
                sources = (*sources, *context.sources)
                derived_mark = (
                    Caveat(
                        CAVEAT_EMULATOR_LIST_DERIVED,
                        "the readable layers declare only part of the catalogue, so the systems "
                        "the installed cores' own declarations file under join this list — the "
                        "sealed bundled layer may declare a different set",
                        {},
                    ),
                )
        view = _PlatformView(
            declared,
            frozenset(vocabulary_backed),
            {},
            sources,
            (*snapshot.findings, *snapshot.tail, *derived_mark),
        )
        return view, version

    def _catalogue_answer(
        self, system: str, *, content_path: str | None = None
    ) -> tuple[CatalogueAnswer, str | None]:
        """EmuDeck's own catalogue answer — one snapshot of the ES-DE sources.

        The contract this fills in is on
        :meth:`_CatalogueQueries.emulators_for`; what is EmuDeck's alone is
        where the answer comes from: the marker, the on-disk ES-DE layers and
        the system's gamelist, each read once here. An empty entry list in the
        sealed state is **not** "the frontend knows none" — the sealed caveat
        is the code that keeps those apart, and a system the readable layers
        do not declare answers with the *derived* enumeration instead (issue
        #133): the installed cores' own declarations, stated as derived,
        because the sealed layer may declare a different list. The version
        travelling back is the backend checkout's HEAD, exactly as on
        :meth:`_systems_answer`.
        """
        version = self._observed_backend_head()
        snapshot = self._esde_snapshot(system)
        if snapshot.refusal is not None:
            return CatalogueAnswer(caveats=snapshot.refusal), version
        if system not in snapshot.by_system and not snapshot.complete:
            context = self._derived_context(snapshot)
            entries, derived = _derived_catalogue_entries(self, context, system)
            return (
                CatalogueAnswer(
                    entries,
                    context.sources,
                    (*snapshot.findings, *snapshot.tail, *derived),
                ),
                version,
            )
        anchor = (
            self._esde_system_dir(
                snapshot.by_system, system, complete=snapshot.complete, relocated=snapshot.relocated
            )
            if content_path is not None
            else _NO_ANCHOR_NEEDED
        )
        return (
            CatalogueAnswer(
                _entries_from(
                    self,
                    _declared_entries(snapshot.by_system, system),
                    self._gamelist_selections(system),
                    system_roms_dir=anchor.directory,
                    content_path=content_path,
                ),
                (_CATALOGUE_SOURCE_EXCLUSIVE if snapshot.exclusive else self._CATALOGUE_SOURCE,),
                (*snapshot.findings, *snapshot.tail, *anchor.caveats),
            ),
            version,
        )

    def _derived_context(self, snapshot: _EsdeSnapshot) -> FirmwareContext:
        """The core enumeration behind a derived answer — from the snapshot's own cfg read.

        ``findings`` stay empty here on purpose: the snapshot already carries
        this query's health, and the context is consumed for its cores alone —
        its caveats never reach the answer, which states its own.
        """
        sandbox, environment_sources = self._cfg_sandbox()
        return _retroarch_firmware_context(
            sandbox=sandbox,
            global_text=snapshot.companion_text,
            cfg_label=RETROARCH_CFG,
            retroarch_config_dir=self._retroarch_config_dir(),
            findings=(),
            arrangement_version=None,
            extra_sources=environment_sources,
        )


    def _launchable_answer(
        self, system: str, content_path: str
    ) -> tuple[LaunchabilityAnswer, str | None]:
        """EmuDeck's launchability answer — the same snapshot its catalogue answer takes.

        ``complete`` is the snapshot's own flag: with the bundled layer sealed
        inside the AppImage, a system the readable layers do not declare may
        still exist, so the verdict stays unknown on the sealed statement
        alone rather than claiming the frontend knows no such system. An
        overlay-declared system answers fully — per ES-DE's merge semantics
        the overlay replaces a same-name bundled system entirely, so its
        accept-list is exactly what the frontend uses.
        """
        extension = esde_extension(content_path)
        version = self._observed_backend_head()
        snapshot = self._esde_snapshot(system)
        if snapshot.refusal is not None:
            return (
                LaunchabilityAnswer(
                    verdict=VERDICT_UNKNOWN, extension=extension, caveats=snapshot.refusal
                ),
                version,
            )
        anchor = self._esde_system_dir(
            snapshot.by_system, system, complete=snapshot.complete, relocated=snapshot.relocated
        )
        entries = _entries_from(
            self,
            _declared_entries(snapshot.by_system, system),
            self._gamelist_selections(system),
            system_roms_dir=anchor.directory,
            content_path=content_path,
        )
        declaration = snapshot.by_system.get(system)
        core_reader = _EntryCoreReader(
            self._machine, self._companion_cfg_path(), self._cfg_sandbox
        )
        verdict, entry, alternatives, sources, own = _launchability_verdict(
            system=system,
            extension=extension,
            declaration=declaration,
            entries=entries,
            complete=snapshot.complete,
            core_info_for=core_reader,
        )
        return (
            LaunchabilityAnswer(
                verdict=verdict,
                extension=extension,
                accepted=declaration.extensions if declaration is not None else (),
                entry=entry,
                alternatives=alternatives,
                sources=(
                    _CATALOGUE_SOURCE_EXCLUSIVE if snapshot.exclusive else self._CATALOGUE_SOURCE,
                    *core_reader.sources,
                    *sources,
                ),
                caveats=(*snapshot.findings, *snapshot.tail, *own, *anchor.caveats),
            ),
            version,
        )

    def _rom_location_answer(self, system: str) -> tuple[RomPlacement, str | None]:
        """EmuDeck's ROM placement — the overlay's declaration, resolved ES-DE's way.

        An overlay-declared system answers fully: per ES-DE's merge semantics
        the overlay replaces a same-name bundled system entirely, so its
        ``<path>`` and ``<extension>`` are exactly what the frontend uses. A
        system the readable layers do not declare answers nothing *carrying
        the sealed caveat* — the declaration may sit in the sealed layer,
        which is a different claim from ``rom-path-undeclared``'s "the
        catalogue was read and declares none". The version travelling back is
        the backend checkout's HEAD, exactly as on :meth:`_systems_answer`.
        """
        version = self._observed_backend_head()
        snapshot = self._esde_snapshot(system)
        if snapshot.refusal is not None:
            return RomPlacement(caveats=snapshot.refusal), version
        declaration = snapshot.by_system.get(system)
        resolved = self._esde_system_dir(
            snapshot.by_system, system, complete=snapshot.complete, relocated=snapshot.relocated
        )
        placement = RomPlacement(
            extensions=() if declaration is None else declaration.extensions,
            sources=(
                _CATALOGUE_SOURCE_EXCLUSIVE if snapshot.exclusive else self._CATALOGUE_SOURCE,
                *resolved.sources,
            ),
            caveats=(*snapshot.findings, *snapshot.tail, *resolved.caveats),
        )
        if resolved.directory is None:
            return placement, version
        # Same rule as RetroDECK's: every resolution branch that carries a
        # caveat also answers directory=None, so a resolved directory has
        # nothing to drop here — and the same shared link view backs it.
        physical_dir, link_caveats = _link_view(self._machine, resolved.directory)
        return (
            _dc_replace(
                placement,
                dir=resolved.directory,
                physical_dir=physical_dir,
                caveats=(*snapshot.findings, *snapshot.tail, *link_caveats),
            ),
            version,
        )

    def _entry_caveats_for(self, spec: EmulatorSpec, content_path: str) -> tuple[Caveat, ...]:
        """What the ES-DE side says about *this* game being launched by *this* entry.

        The same question RetroDECK's entry route asks, over EmuDeck's
        sources, and it re-reads them — the handle is live, and the machine
        may have changed since the catalogue handed the entry out. The
        catalogue-status statement rides here too: sealed whenever the
        bundled layer stayed sealed (the entry came out of that partly-sealed
        catalogue, and the anchor the per-game check needs may be declared in
        the part nobody could read), exclusive when the overlay excluded the
        bundled layer instead.

        Deliberately **not** :meth:`_esde_snapshot`: that would read the
        marker and the companion cfg a second time inside one placement query
        (:meth:`_query` already read both), and its refusal shapes carry the
        health findings this placement already states. Only the ES-DE files
        this check itself needs are read here.
        """
        if not self._esde_present():
            return (self._catalogue_absence(),)
        portable = self._relocation_caveat()
        relocation = () if portable is None else (portable,)
        by_system, complete, shadow_broken, exclusive, catalogue_invalid = (
            self._read_esde_catalogue()
        )
        if shadow_broken:
            return (*_catalogue_unread_caveat(spec.system), *relocation)
        if exclusive:
            status: tuple[Caveat, ...] = self._catalogue_exclusive(spec.system)
        else:
            status = () if complete else (self._catalogue_sealed_caveat(spec.system),)
        if catalogue_invalid is not None:
            status = (catalogue_invalid, *status)
        anchor = self._esde_system_dir(
            by_system, spec.system, complete=complete, relocated=bool(relocation)
        )
        override_label = (
            None
            if anchor.directory is None
            else _match_per_game(
                self._gamelist_selections(spec.system), content_path, system_roms_dir=anchor.directory
            )
        )
        if override_label is None or override_label == spec.label:
            return (*status, *relocation, *anchor.caveats)
        return (*status, *relocation, *anchor.caveats, _per_game_override_caveat(override_label, spec))

    def entry_savefile_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> SavefilePlacement | Unresolved:
        """The entry route behind :meth:`EmulatorEntry.savefile_location` — EmuDeck's wiring.

        The placement itself is the companion RetroArch's, exactly as the
        direct question answers it; what the entry adds is its own catalogue
        caveats and, when content is named, the per-game override check.
        A standalone entry goes through the launcher route: EmuDeck's
        catalogue launches these emulators through per-emulator scripts, and
        an established launcher leads to the same save card RetroDECK's token
        does — read against this arrangement's own config tree.
        """
        if spec.kind != KIND_LIBRETRO:
            return self._standalone_entry_savefile(spec, entry_caveats, content_path=content_path)
        placement = _retroarch_savefile_location(
            self._machine,
            self._query(
                content_path=content_path,
                core_so=spec.core_so,
                system=spec.system,
                extra_caveats=entry_caveats,
            ),
        )
        if content_path is None:
            return placement
        extra = self._entry_caveats_for(spec, content_path)
        return _entry_savefile_with_caveats(placement, extra)

    def _standalone_entry_savefile(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...],
        *,
        content_path: str | None,
    ) -> SavefilePlacement | Unresolved:
        """A standalone entry's save answer, resolved the way the launch resolves.

        Which binary a launch runs decides which configuration tree speaks,
        and that probe is :meth:`_standalone_launch_gate` — one gate for all
        four questions, so the save answer and the texture, mod and firmware
        answers about the same entry cannot come to different conclusions or
        word the same refusal two ways. What is left here is the part that is
        the save route's own: the card has to cover this entry's system.
        """
        launch = self._standalone_launch_identity(spec.command)
        card = lookup_standalone_save_card(launch.token)
        if launch.probe_name is None or card is None or spec.system not in card.systems:
            return _standalone_savefile_unresolved(spec)
        gate = self._standalone_launch_gate(spec)
        if gate.homes is None:
            assert gate.variant is not None  # a card was found, so the launch identified one
            return _emudeck_variant_unresolved(spec, card.token, gate.variant, gate.why or "")
        homes = gate.homes
        _, marker_issues = self._read_marker()
        extra = (
            self._entry_caveats_for(spec, content_path) if content_path is not None else ()
        )
        return _standalone_savefile_placement(
            self._machine,
            card=card,
            homes=homes,
            sandbox=self._standalone_sandbox(),
            system=spec.system,
            command=spec.command,
            extra_caveats=(
                *entry_caveats,
                *extra,
                *marker_issues,
                *arrangement_caveats(self.kind, observed_version=self._observed_backend_head()),
            ),
            content_path=content_path,
        )

    def _launcher_binary_variant(self, name: str) -> str:
        """Which binary the launcher would pick — its probe, performed as reads.

        ``getAppImage`` searches ``$emusFolder`` (``~/Applications``,
        vars.sh:4-5) for ``<Name>*.AppImage`` case-insensitively;
        ``getFlatpak`` greps the installed flatpak ids for the name
        (cemu.sh:37-65). The flatpak probe reads the two flatpak app
        directories, which is the installed-set that listing enumerates. An
        unreadable directory makes the pick unestablished rather than "none":
        the launcher would still look there.

        Between those two sits the extracted binary: EmuDeck unpacks some
        emulators out of their AppImage and keeps the executable at
        ``~/Applications/<Name>/<Name>`` (emuDeckVita3K.sh:21-24), which is
        exactly where ES-DE's own find rules look right after the AppImage
        patterns (es_find_rules.xml), and which ``vita3k.sh`` finds by name
        inside that directory.
        """
        wanted = name.casefold()
        apps = self._machine.glob(os.path.join(self._home, "Applications", "*"))
        for path in apps.matches:
            base = os.path.basename(path).casefold()
            if base.startswith(wanted) and base.endswith(".appimage"):
                return _EMUDECK_VARIANT_APPIMAGE
        unpacked = self._unpacked_binary_variant(wanted, apps.matches)
        if unpacked is not None:
            return unpacked
        if apps.status != GLOB_COMPLETE:
            return _EMUDECK_VARIANT_UNKNOWN
        flatpak_roots = (
            _FLATPAK_DEPLOY_SYSTEM,
            os.path.join(self._home, _FLATPAK_DEPLOY_USER),
        )
        incomplete = False
        for root in flatpak_roots:
            listing = self._machine.glob(os.path.join(root, "*"))
            if any(wanted in os.path.basename(p).casefold() for p in listing.matches):
                return _EMUDECK_VARIANT_FLATPAK
            incomplete = incomplete or listing.status != GLOB_COMPLETE
        if incomplete:
            return _EMUDECK_VARIANT_UNKNOWN
        return _EMUDECK_VARIANT_PROTON

    def _standalone_xdg_homes(self) -> _XdgHomes:
        """The XDG bases an EmuDeck AppImage emulator reads — the host's own.

        Nothing sandboxes an AppImage: EmuDeck's Cemu setup writes the config
        it manages at ``${HOME}/.config/Cemu/settings.xml``
        (emuDeckCemu.sh:13), which is the plain XDG default.
        """
        return _XdgHomes(
            data=os.path.join(self._home, _XDG_DATA_SUFFIX),
            config=os.path.join(self._home, _XDG_CONFIG_DIRNAME),
        )

    def _standalone_launch_identity(self, command: str) -> _StandaloneLaunch:
        """What this command identifies — card token, probe name, launcher args.

        Two spellings identify an emulator on EmuDeck, and its overlays
        genuinely use both: the ES-DE ``%EMULATOR_…%`` token, and a
        ``tools/launchers/<name>.sh`` script. Either way the binary is picked
        at run time (ES-DE's find rules for the token, the script's own probe
        for the launcher), so both go through the same variant gate — except
        a script that pins its binary, which carries that pin instead. The
        probe name is the token casefolded, or the script's basename; the
        args are the launcher's (a token entry has none — ``-w`` is the
        launcher's spelling).
        """
        token = emulator_token(command)
        if token is not None:
            return _StandaloneLaunch(token, token.casefold(), ())
        launcher = _emudeck_launcher(command)
        if launcher is None:
            return _StandaloneLaunch(None, None, ())
        name, args = launcher
        return _StandaloneLaunch(
            _EMUDECK_LAUNCHER_CARDS.get(name),
            name,
            args,
            _EMUDECK_LAUNCHER_PINNED_VARIANTS.get(name),
        )

    def _launch_variant(self, launch: _StandaloneLaunch) -> str:
        """The variant this launch runs — the script's own pin, else the probe."""
        if launch.pinned_variant is not None:
            return launch.pinned_variant
        assert launch.probe_name is not None  # callers gate on the identity first
        return self._launcher_binary_variant(launch.probe_name)

    def _unpacked_binary_variant(
        self, wanted: str, apps: tuple[str, ...]
    ) -> str | None:
        """The variant an unpacked executable answers, or ``None`` where none does.

        EmuDeck unpacks some emulators out of their AppImage and keeps the
        executable at ``~/Applications/<Name>/<Name>``
        (emuDeckVita3K.sh:21-24), which is the path ES-DE's own find rule
        looks for right after the AppImage patterns. The directory alone is
        not the variant: the executable inside it is what a launch runs, and a
        directory that cannot be listed leaves the pick unestablished rather
        than answering "not here".
        """
        for path in apps:
            if os.path.basename(path).casefold() != wanted:
                continue
            inner = self._machine.glob(os.path.join(path, "*"))
            if any(os.path.basename(p).casefold() == wanted for p in inner.matches):
                return _EMUDECK_VARIANT_BINARY
            if inner.status != GLOB_COMPLETE:
                return _EMUDECK_VARIANT_UNKNOWN
        return None

    def _homes_for_token(self, variant: str, token: str) -> _XdgHomes | None:
        """The XDG bases the picked binary reads, or ``None`` where none are established.

        The AppImage variant reads the host's own tree (emuDeckCemu.sh:13,
        vars.sh:4-5), and so does the extracted binary — nothing sandboxes
        either, and EmuDeck writes the one it unpacks at the plain XDG default
        (emuDeckVita3K.sh:7). The flatpak variant reads the app's own homes
        below ``~/.var/app`` — established where the settings table names the
        app id this emulator installs as; EmuDeck grants every emulator
        flatpak ``--filesystem=host`` (installEmuFP.sh:33), so paths
        configured inside those homes stay host paths. Proton, and an
        emulator no arrangement is established to install as a flatpak, have
        no established bases.

        The id comes from the table rather than from a card because it is the
        identity of the *installation*, and every question asks it: the save
        route holds a save card while asking, the texture and mod routes hold
        their own, and the firmware route holds none. Reading it off the save
        card meant an emulator without one could not reach its own trees at
        all — MAME's savestate answer refused a launch it could have read
        (#288).
        """
        if variant in (_EMUDECK_VARIANT_APPIMAGE, _EMUDECK_VARIANT_BINARY):
            return self._standalone_xdg_homes()
        if variant != _EMUDECK_VARIANT_FLATPAK:
            return None
        app_id = emulator_settings.installed_flatpak(token)
        if app_id is None:
            return None
        app_dir = os.path.join(self._home, ".var", "app", app_id)
        return _XdgHomes(
            data=os.path.join(app_dir, "data"),
            config=os.path.join(app_dir, "config"),
            flatpak=app_id,
            xdg_pinned=True,
        )

    def _variant_reason(self, launch: _StandaloneLaunch, variant: str) -> str:
        """Why a launch whose variant *is* established still has no trees to read.

        One text per case, shared by every route that asks: the save answer,
        the texture answer and the mod answer describe the same launch, and a
        caller comparing them must not find three tellings of one fact.
        """
        if variant == _EMUDECK_VARIANT_PROTON:
            return (
                "no AppImage sits under ~/Applications, so the launch falls through to "
                "the Windows build under Proton, whose configuration lives inside "
                "the Proton prefix and is not read (a later slice)"
            )
        if launch.pinned_variant is not None:
            return (
                "the launcher script runs the installed flatpak outright, and the settings "
                "table names no app id whose configuration trees this emulator's launch "
                "would hang off"
            )
        return (
            "no AppImage sits under ~/Applications, so the launch falls through to "
            "the installed flatpak, whose own config tree is not established "
            "(a later slice)"
        )

    def _standalone_launch_gate(self, spec: EmulatorSpec) -> "_EmuDeckGate":
        """The variant gate, performed once for whichever question is asking.

        The save route has run this since #219 and the firmware route since
        #220; the texture and mod routes did not, which is why every
        standalone entry refused them here. What the gate answers is the same
        for all four: which binary this launch runs, and which XDG bases that
        binary reads — or, where nothing is established, the refusal that
        names the variant instead of an invented tree.
        """
        launch = self._standalone_launch_identity(spec.command)
        if launch.token is None or launch.probe_name is None:
            return _EmuDeckGate(launch, None, None, None)
        if "-w" in launch.args:
            return _EmuDeckGate(
                launch,
                _EMUDECK_VARIANT_PROTON,
                None,
                f"{launch.probe_name}.sh -w runs the Windows build under Proton, whose "
                "configuration lives inside the Proton prefix and is not read (a later slice)",
            )
        variant = self._launch_variant(launch)
        if variant == _EMUDECK_VARIANT_UNKNOWN:
            return _EmuDeckGate(
                launch,
                variant,
                None,
                "the launch's own binary probe could not be performed — the directories it "
                "searches were not readable, so which binary would run is not established",
            )
        homes = self._homes_for_token(variant, launch.token)
        if homes is None:
            return _EmuDeckGate(launch, variant, None, self._variant_reason(launch, variant))
        return _EmuDeckGate(launch, variant, homes, None)

    def standalone_firmware_token(self, command: str) -> str | None:
        """The command's word, variant-gated — EmuDeck's own reading.

        A token only where the launch would really run the binary whose trees
        the cards describe: identified by token or allowlisted launcher, not
        forced to Proton by ``-w``, and picking a binary whose homes are
        established — the AppImage, or a flatpak whose app id the settings
        table names. Every other launch answers ``None`` and stays honestly
        unsupported — the same gating the save route applies, because it is
        the same question about the same command.
        """
        launch = self._standalone_launch_identity(command)
        if launch.token is None or launch.probe_name is None or "-w" in launch.args:
            return None
        variant = self._launch_variant(launch)
        # Established homes are exactly what makes the token stand, so the gate
        # is the homes themselves rather than a second reading of the same
        # facts: the AppImage and the extracted binary read the host's tree,
        # the flatpak reads the app's own where the table names one, and
        # nothing else is established.
        homes = self._homes_for_token(variant, launch.token)
        return launch.token if homes is not None else None

    def standalone_firmware_homes(self, command: str) -> _XdgHomes | None:
        """The homes the gated launch reads — per entry, because the variant is.

        The same identity and gate as the token: an ungated launch answers
        ``None`` (its token is ``None`` too, so nothing consumes homes), the
        AppImage answers the host pair, and a flatpak whose app id the
        settings table names answers the app's own trees below ``~/.var/app``.
        """
        launch = self._standalone_launch_identity(command)
        if launch.token is None or launch.probe_name is None or "-w" in launch.args:
            return None
        return self._homes_for_token(self._launch_variant(launch), launch.token)

    def _standalone_sandbox(self) -> _Sandbox:
        """No path translation: both established variants read host paths.

        Nothing sandboxes an AppImage, and EmuDeck grants every emulator
        flatpak ``--filesystem=host`` (installEmuFP.sh:33) — either way a
        configured path means the host path it spells, and ``~`` expands to
        the host home (flatpak leaves ``$HOME`` itself untouched).
        """
        return _Sandbox(self._machine, self._home, None, expansion_home=self._home)

    def entry_savestate_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> SavestatePlacement | SavestateAbsence | Unresolved:
        """The savefile entry route's twin — same sources, the savestate keys.

        A standalone entry goes through the launcher route the save answer
        goes through: the same variant gate, the savestate card the token
        leads to, and the same refusals where nothing is established (#225).
        """
        if spec.kind != KIND_LIBRETRO:
            return self._standalone_entry_savestate(spec, entry_caveats, content_path=content_path)
        placement = _retroarch_savestate_location(
            self._machine,
            self._query(content_path=content_path, core_so=spec.core_so, extra_caveats=entry_caveats),
        )
        if content_path is None:
            return placement
        extra = self._entry_caveats_for(spec, content_path)
        return _entry_savestate_with_caveats(placement, extra)

    def _standalone_entry_savestate(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...],
        *,
        content_path: str | None,
    ) -> SavestatePlacement | SavestateAbsence | Unresolved:
        """A standalone entry's states answer, resolved the way the launch resolves.

        :meth:`_standalone_entry_savefile`'s twin: the same launch identity,
        the same variant gate, and the savestate card where the save route
        holds the save card — so the two questions about one entry can never
        come to different conclusions about which binary runs. An absence
        card answers BEFORE the variant gate (#284): the stated no is about
        the emulator, not about which binary this launch would pick, so a
        launch whose variant is unestablished still gets the answer instead
        of a refusal about trees the answer never needed.
        """
        launch = self._standalone_launch_identity(spec.command)
        card = lookup_standalone_savestate_card(launch.token)
        if launch.probe_name is None or card is None or spec.system not in card.systems:
            return _standalone_savestate_unresolved(spec)
        # Computed before the absence branch, the same way the RetroDeck route
        # does: a per-game override is a statement about which emulator runs
        # at all, so the stated no needs it exactly as a placement would.
        extra = (
            self._entry_caveats_for(spec, content_path) if content_path is not None else ()
        )
        if card.absent is not None:
            return _savestate_absence_answer(
                card,
                entry=(*entry_caveats, *extra),
                arrangement=arrangement_caveats(
                    self.kind, observed_version=self._observed_backend_head()
                ),
            )
        gate = self._standalone_launch_gate(spec)
        if gate.homes is None:
            assert gate.variant is not None  # a card was found, so the launch identified one
            return _emudeck_variant_unresolved(spec, card.token, gate.variant, gate.why or "")
        homes = gate.homes
        _, marker_issues = self._read_marker()
        return _standalone_savestate_placement(
            self._machine,
            card=card,
            homes=homes,
            sandbox=self._standalone_sandbox(),
            system=spec.system,
            command=spec.command,
            extra_caveats=(
                *entry_caveats,
                *extra,
                *marker_issues,
                *arrangement_caveats(self.kind, observed_version=self._observed_backend_head()),
            ),
            content_path=content_path,
        )

    def entry_texture_pack_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> TexturePlacement | Unresolved:
        """The texture-pack entry route — same sources, the entry's own caveats.

        A standalone entry answers through the same variant gate the save
        route uses: which binary the launch runs decides which XDG tree its
        packs sit in, and EmuDeck's per-emulator installs are exactly why that
        cannot be one arrangement-wide pair. Where the variant is not
        established the refusal names it, rather than answering from a tree
        the binary never reads.
        """
        if spec.kind != KIND_LIBRETRO:
            return self._standalone_entry_texture(spec, entry_caveats, content_path=content_path)
        placement = _retroarch_texture_pack_location(
            self._machine,
            self._query(content_path=content_path, core_so=spec.core_so, extra_caveats=entry_caveats),
        )
        if content_path is None:
            return placement
        return _entry_texture_with_caveats(placement, self._entry_caveats_for(spec, content_path))

    def entry_mod_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> ModPlacement | Unresolved:
        """The mod entry route — the texture route's twin, gate included."""
        if spec.kind != KIND_LIBRETRO:
            return self._standalone_entry_mod(spec, entry_caveats, content_path=content_path)
        placement = _retroarch_mod_location(
            self._machine,
            self._query(content_path=content_path, core_so=spec.core_so, extra_caveats=entry_caveats),
        )
        if content_path is None:
            return placement
        return _entry_mod_with_caveats(placement, self._entry_caveats_for(spec, content_path))

    def _standalone_entry_texture(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...],
        *,
        content_path: str | None,
    ) -> TexturePlacement | Unresolved:
        """A standalone entry's texture answer, resolved the way the launch resolves.

        The card is the same one RetroDECK's token reaches; what differs is
        the pair of bases it hangs off, and that is a property of this launch
        rather than of the arrangement. A card whose directory is a
        configuration value reads that configuration inside these homes too,
        so the whole answer — directory and switch alike — comes off the tree
        the picked binary actually opens.
        """
        gate = self._standalone_launch_gate(spec)
        card = lookup_standalone_texture_card(gate.launch.token)
        if card is None or gate.variant is None:
            return _standalone_texture_unresolved(spec)
        if gate.homes is None:
            assert gate.why is not None  # a gate without homes always says why
            return _emudeck_variant_unresolved(spec, card.token, gate.variant, gate.why)
        return _standalone_texture_placement(
            self._machine,
            card=card,
            homes=gate.homes,
            sandbox=self._standalone_sandbox(),
            extra_caveats=self._standalone_entry_caveats(spec, entry_caveats, content_path),
        )

    def _standalone_entry_mod(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...],
        *,
        content_path: str | None,
    ) -> ModPlacement | Unresolved:
        """A standalone entry's mod answer — the texture route's twin, gate included."""
        gate = self._standalone_launch_gate(spec)
        card = lookup_standalone_mod_card(gate.launch.token)
        if card is None or gate.variant is None:
            return _standalone_mod_unresolved(spec)
        if gate.homes is None:
            assert gate.why is not None  # a gate without homes always says why
            return _emudeck_variant_unresolved(spec, card.token, gate.variant, gate.why)
        return _standalone_mod_placement(
            self._machine,
            card=card,
            homes=gate.homes,
            sandbox=self._standalone_sandbox(),
            extra_caveats=self._standalone_entry_caveats(spec, entry_caveats, content_path),
        )

    def _standalone_entry_caveats(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...],
        content_path: str | None,
    ) -> tuple[Caveat, ...]:
        """What every standalone answer of this arrangement carries beside its own.

        The entry's own caveats, the per-game override check where content is
        named, the marker's health, and the arrangement's version evidence —
        the same four the save route assembles, in the same order, so two
        answers about one entry never disagree about the machine they were
        read on.
        """
        _, marker_issues = self._read_marker()
        extra = self._entry_caveats_for(spec, content_path) if content_path is not None else ()
        return (
            *entry_caveats,
            *extra,
            *marker_issues,
            *arrangement_caveats(self.kind, observed_version=self._observed_backend_head()),
        )

    def _retroarch_config_dir(self) -> str:
        return os.path.join(self._home, ".var", "app", self._RA_APP_ID, "config", "retroarch")

    def _cfg_sandbox(self) -> tuple[_Sandbox, tuple[str, ...]]:
        """The sandbox the companion cfg resolves through — the bare Flatpak's own override files.

        EmuDeck's RetroArch *is* the ``org.libretro.RetroArch`` Flatpak, so
        the files that speak for its runs are that app's (issue #101), read
        with :func:`_flatpak_cfg_sandbox`'s composition — the same one
        RetroDECK's handle reads its own with. EmuDeck's scripts pin nothing
        here: the flatpak XDG pin alone is what keeps the config home in
        place, and ``HOME`` remains the one seam that reaches a cfg ``~``.
        """
        return _flatpak_cfg_sandbox(self._machine, self._home, self._RA_APP_ID)

    def _query(
        self,
        *,
        content_path: str | None,
        core_so: str | None,
        system: str | None = None,
        extra_caveats: tuple[Caveat, ...] = (),
    ) -> _SaveQuery:
        """The placement question, over one read of the companion cfg.

        The version the machine states about itself is the backend checkout's
        HEAD (:meth:`_observed_backend_head`), read once here and stated
        through both channels — the per-card comparison and the
        arrangement-level evidence — so the two can never weigh different
        readings of it. The sandbox arrives with its ``~`` base composed from
        the bare Flatpak's override files (:meth:`_cfg_sandbox`) — this
        query's one read of those files — and every resolution goes through
        that one sandbox, the core path included.
        """
        settings, marker_issues = self._read_marker()
        global_cfg_path = self._companion_cfg_path()
        cfg = self._machine.read_text(global_cfg_path)
        health = self._health_from(settings, marker_issues, cfg.status)
        version = self._observed_backend_head()
        context = _flatpak_query_context(self._machine, self._home, self._RA_APP_ID)
        sandbox = context.sandbox
        return _SaveQuery(
            sandbox=sandbox,
            global_cfg_path=global_cfg_path,
            global_text=cfg.text,
            cfg_label=RETROARCH_CFG,
            override_config_dir=os.path.join(self._retroarch_config_dir(), "config"),
            defaults=UPSTREAM_DEFAULTS,
            content_path=content_path,
            core_so=core_so,
            core_path_resolver=lambda so: _core_path_from(sandbox, cfg.text, so),
            arrangement="emudeck",
            arrangement_version=version,
            system=system,
            extra_sources=context.sources,
            extra_caveats=(
                *extra_caveats,
                *health.issues,
                *arrangement_caveats(self.kind, observed_version=version),
            ),
            revocation=context.revocation,
        )

    def savefile_location(
        self,
        *,
        content_path: str | None = None,
        core_so: str | None = None,
        system: str | None = None,
    ) -> SavefilePlacement | Unresolved:
        """Where EmuDeck's RetroArch keeps the save — resolved from the bare Flatpak cfg.

        *system* is the content's system in ES-DE's vocabulary
        (:func:`atlas.known_systems`). It is what keys a core's recorded file
        set: one core is not one behaviour, so without it the names stay
        unstated unless every system the record covers agrees. Naming it is the
        only way to get file names on an arrangement with no frontend
        catalogue, and it is never guessed from the core — a core's own
        metadata says which systems it *can* run, never which one this content
        is.
        """
        return _retroarch_savefile_location(
            self._machine,
            self._query(content_path=content_path, core_so=core_so, system=system),
        )

    def savestate_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> SavestatePlacement | Unresolved:
        """Where EmuDeck's RetroArch keeps the savestates — the same cfg, the state keys.

        At the current pin both directions derive from ``savesPath``:
        ``RetroArch_setupSaves`` writes ``savestate_directory`` and
        ``savefile_directory`` as ``$savesPath/retroarch/{states,saves}`` into
        the global cfg (``emuDeckRetroArch.sh:222-230`` @ ``863ab69``), after
        symlinking those very paths into the Flatpak's own config tree — the
        cfg names the symlink, the bytes live behind it, and the placement's
        ``dir``/``physical_dir`` pair carries both truthfully. A prior
        generation (@ ``acc45fc``) hardcoded the states path into the Flatpak
        tree via ``RetroArch_maincfg.sh:3053`` — a writer that at the current
        pin has no caller in the Linux backend and targets an override file no
        launch path reads. The handle reads the live cfg either way; the
        installer history is context, not an input.
        """
        return _retroarch_savestate_location(
            self._machine, self._query(content_path=content_path, core_so=core_so)
        )

    def screenshot_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> ScreenshotPlacement | Unresolved:
        """Where EmuDeck's RetroArch writes screenshots — the same cfg, its own keys."""
        return _retroarch_screenshot_location(
            self._machine, self._query(content_path=content_path, core_so=core_so)
        )

    def texture_pack_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> TexturePlacement | Unresolved:
        """Where EmuDeck's RetroArch reads texture packs from, per core.

        The answer is the emulator's own read location and nothing else. EmuDeck
        keeps browsable trees of its own — ``Emulation/texturepacks``,
        ``Emulation/hdpacks``, ``storage/<emulator>/textures`` — wired the
        opposite way round from RetroDECK's: the links live in the shared tree
        and point *into* the emulator's real directory
        (``functions/helperFunctions.sh:479-506`` @ ``863ab69``), so nothing on
        any emulator's read path passes through them and nothing here models
        them. For the cores this arrangement wires, the read location is the
        default below ``system_directory``, which the installer points at its own
        bios root (``functions/EmuScripts/emuDeckRetroArch.sh:216``) and which
        this route reads live like every other setting.
        """
        return _retroarch_texture_pack_location(
            self._machine, self._query(content_path=content_path, core_so=core_so)
        )

    def mod_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> ModPlacement | Unresolved:
        """Where EmuDeck's RetroArch reads mods from, per core.

        EmuDeck wires no mod hub at all — its installer creates no mods root
        and links no emulator's mod directory anywhere — so the answer is the
        emulator's own read location and nothing else, which for the cores it
        deploys is the default below the ``system_directory`` it points at its
        own bios root.
        """
        return _retroarch_mod_location(
            self._machine, self._query(content_path=content_path, core_so=core_so)
        )

    def soft_patch_candidates(
        self, content_path: str, *, core_so: str | None = None
    ) -> SoftPatchAnswer | Unresolved:
        """Which patch files EmuDeck's RetroArch would apply to *content_path*.

        EmuDeck runs the ``org.libretro.RetroArch`` Flatpak rather than a
        RetroArch of its own, and nobody has read that binary for its patch
        flags — so every candidate comes back unestablished
        (``patch-formats-unestablished``) while the file names, which follow
        from the content path alone, are exact.
        """
        return _retroarch_soft_patch_candidates(
            self._machine,
            self._query(content_path=content_path, core_so=core_so),
            build=lookup_soft_patch_build(self.kind),
        )

    def _firmware_context_from(
        self, settings: dict[str, str], marker_issues: tuple[Caveat, ...], cfg: ReadResult
    ) -> FirmwareContext:
        """The cfg is the truth here too: ``settings.sh`` names a ``biosPath``,
        but what RetroArch actually hands its cores is ``system_directory``.

        The companion cfg is read once and answers both questions asked of it —
        its text builds the context, its status decides the companion health
        finding — so the two can never describe different revisions of it. The
        marker snapshot arrives for the same reason: the caller read it once
        and every part of its answer describes that one revision. The sandbox
        arrives with its ``~`` base composed from the bare Flatpak's override
        files (:meth:`_cfg_sandbox`) — this context's one read of those files.
        """
        sandbox, environment_sources = self._cfg_sandbox()
        return _retroarch_firmware_context(
            sandbox=sandbox,
            global_text=cfg.text,
            cfg_label=RETROARCH_CFG,
            retroarch_config_dir=self._retroarch_config_dir(),
            findings=self._health_from(settings, marker_issues, cfg.status).issues,
            # ``settings.sh`` names no EmuDeck version — the backend
            # checkout's HEAD is the version this machine states about itself.
            arrangement_version=self._observed_backend_head(),
            standalone_homes=self._standalone_xdg_homes(),
            standalone_sandbox=self._standalone_sandbox(),
            extra_sources=environment_sources,
        )

    def _read_firmware_context(self) -> FirmwareContext:
        settings, marker_issues = self._read_marker()
        cfg = self._machine.read_text(self._companion_cfg_path())
        return self._firmware_context_from(settings, marker_issues, cfg)

    def firmware_for_system(self, system: str, *, verify: bool = False) -> FirmwareAnswer:
        """Which emulators this EmuDeck's ES-DE offers for *system*, and what each wants.

        RetroDECK's route mirrored over this arrangement's sources: the ES-DE
        on disk is the enumeration, assembled from this query's own snapshot —
        marker, companion cfg and the on-disk ES-DE layers are each read once
        here and handed on. What is EmuDeck's alone is the catalogue's third
        state: the bundled layer is ordinarily sealed inside the AppImage, so
        the catalogue travels with its pinned sealed statement as the seam's
        ``hole`` — stated by the resolver on every answer the catalogue
        informs, while an id the readable layers do not declare answers empty
        as a look that failed, never as "no emulator covers this system".

        With no ES-DE on this disk the catalogue-less behavior stands
        unchanged: the derived enumeration, stated as derived. A broken
        resource shadow is the unreadable catalogue, exactly as RetroDECK's
        unreadable bundled layer is. And on every catalogue-informed answer
        the relocation suspicion and the marker cross-check ride adjacent to
        the catalogue-status statement, in the same order every catalogue
        answer pins (sealed, relocation, mismatch).
        """
        settings, marker_issues = self._read_marker()
        cfg = self._machine.read_text(self._companion_cfg_path())
        context = self._stated(self._firmware_context_from(settings, marker_issues, cfg))
        present = self._esde_present()
        if not present or context.root is None:
            # No ES-DE on this disk means the catalogue-less behavior stands;
            # and a root the context could not resolve refuses before any
            # catalogue could inform the answer — the resolver returns the
            # same empty answer either way, so the ES-DE sources are not read
            # for an answer that would discard them.
            return _resolve_for_system(self._machine, context, system=system, verify=verify)
        riders = self._riders(settings, present)
        by_system, complete, shadow_broken, exclusive, catalogue_invalid = (
            self._read_esde_catalogue()
        )
        if shadow_broken:
            # The bundled layer is on disk (the resource shadow) and could not
            # be read: the resolver states the unreadable catalogue, the same
            # statement RetroDECK's unread bundled layer gets.
            catalogue = Catalogue(entries=(), read=False)
        else:
            catalogue = Catalogue(
                entries=_firmware_catalogue_entries(
                    self, by_system, system, self._gamelist_selections(system)
                ),
                hole=None if complete else self._catalogue_sealed_caveat(system),
            )
        answer = _resolve_for_system(
            self._machine, context, system=system, catalogue=catalogue, verify=verify
        )
        # The catalogue-status statements and the riders ride only answers the
        # catalogue informed: an own spelling is answered from the cores on
        # every arrangement, so the resolver never looks at the catalogue for
        # it.
        inserted = (
            *(() if catalogue_invalid is None else (catalogue_invalid,)),
            *(self._catalogue_exclusive(system) if exclusive else ()),
            *riders,
        )
        if not inserted or system in SYSTEMS_WITHOUT_CATALOGUE_ID:
            return answer
        # Adjacent to the catalogue-status statement, which — when one exists
        # (the hole, or the unreadable statement of a broken shadow) — is the
        # first caveat the resolver appends after the context's own; the
        # resolver's answer is (*context.caveats, *its own), per its contract.
        # An exclusive catalogue has no hole, so its statement lands in
        # exactly that slot itself.
        index = len(context.caveats) + (1 if shadow_broken or catalogue.hole is not None else 0)
        return _firmware_with_caveats(
            answer, (*answer.caveats[:index], *inserted, *answer.caveats[index:])
        )


class _RetroArchInstall(_FirmwareQueries, _CatalogueQueries):
    """Shared behavior for a bare RetroArch install (the Flatpak or a native one).

    The saves root comes from the cfg's ``savefile_directory``; when unset, the
    RetroArch platform default applies — ``saves`` under the config tree that
    holds ``retroarch.cfg`` (``platform_unix.c:2133-2134``), never the ROM's own
    directory (the ``runloop.c:8786`` content fallback fires only when the
    effective dir is still empty, which the platform defaults prevent on
    desktop). Bare installs get RetroArch's upstream compile-time defaults,
    under which ``sort_savefiles_enable`` is **true**.
    """

    kinds: tuple[str, ...] = ()
    _app_id: str | None = None

    def _catalogue_absence(self) -> Caveat:
        # A settled fact about the arrangement, not a gap in atlas: RetroArch
        # has no frontend catalogue. The same code the firmware route already
        # uses to say so.
        return Caveat(
            CAVEAT_EMULATOR_CATALOGUE_UNAVAILABLE,
            "a bare RetroArch install ships no frontend catalogue, so there is no launch entry to "
            "answer with — name the core yourself with savefile_location(core_so=...)",
            {"arrangement": "retroarch"},
        )

    def __init__(self, home: str, machine: Machine, cfg_suffix: str) -> None:
        self._home = home
        self._machine = machine
        self._cfg_suffix = cfg_suffix

    def _cfg_path(self) -> str:
        return os.path.join(self._home, self._cfg_suffix)

    def root(self) -> str:
        """The RetroArch config directory (the folder holding ``retroarch.cfg``)."""
        return os.path.dirname(self._cfg_path())

    def _health_from(self, cfg_status: ReadStatus) -> Health:
        path = self._cfg_path()
        if cfg_status == READ_OK:
            return Health()
        if cfg_status == READ_MISSING:
            return Health((Caveat(HEALTH_ISSUE_MARKER_MISSING, f"marker {path} does not exist", {"path": path}),))
        return Health(
            (
                Caveat(
                    HEALTH_ISSUE_CONFIG_UNREADABLE,
                    f"config {path} cannot be read as text ({cfg_status})",
                    {"path": path, "status": cfg_status},
                ),
            )
        )

    def health(self) -> Health:
        """Bare installs: the cfg is the marker — health is its read status."""
        return self._health_from(self._machine.read_text(self._cfg_path()).status)

    def _sandbox(self) -> _Sandbox:
        """This install's cfg spellings — a native install's are the host's own.

        ``_app_id`` is ``None`` for :class:`BareRetroArchNative`: it writes its cfg
        outside any sandbox, so a ``/var/...`` value there is a real host path
        (odd, but the user's), and the existence checks downstream judge it like
        any other. Translating it would move the answer to a directory this
        RetroArch never touches.
        """
        return _Sandbox(self._machine, self._home, self._app_id, expansion_home=self._home)

    def _cfg_sandbox(self) -> tuple[_Sandbox, tuple[str, ...]]:
        """The sandbox a cfg read resolves through, and the statements composing it made.

        The native install's default: the host's own home, and no override
        files anywhere to read — nothing sandboxes this RetroArch, so nothing
        can hand it another ``HOME``. :class:`BareRetroArchFlatpak` overrides
        this with the Flatpak composition (issue #101).
        """
        return self._sandbox(), ()

    def _query(
        self,
        *,
        content_path: str | None,
        core_so: str | None,
        system: str | None = None,
        extra_caveats: tuple[Caveat, ...] = (),
    ) -> _SaveQuery:
        """The placement question, over one read of this install's cfg.

        The sandbox arrives with its ``~`` base composed by
        :func:`_flatpak_query_context` on the Flatpak install — that query's
        one read of the app's override files, whose filesystem tables also
        ride as the revocation check (issue #103) — and every resolution goes
        through that one sandbox, the core path included. A native install
        reads no override files and nothing can revoke its filesystem.
        """
        cfg = self._machine.read_text(self._cfg_path())
        health = self._health_from(cfg.status)
        revocation = None
        if self._app_id is not None:
            context = _flatpak_query_context(self._machine, self._home, self._app_id)
            sandbox, environment_sources = context.sandbox, context.sources
            revocation = context.revocation
        else:
            sandbox, environment_sources = self._cfg_sandbox()
        return _SaveQuery(
            sandbox=sandbox,
            global_cfg_path=self._cfg_path(),
            global_text=cfg.text,
            cfg_label=RETROARCH_CFG,
            override_config_dir=os.path.join(self.root(), "config"),
            defaults=UPSTREAM_DEFAULTS,
            content_path=content_path,
            core_so=core_so,
            system=system,
            core_path_resolver=lambda so: _core_path_from(sandbox, cfg.text, so),
            arrangement="bare",
            arrangement_version=None,
            extra_sources=environment_sources,
            extra_caveats=(*extra_caveats, *health.issues, *arrangement_caveats(self.kind)),
            revocation=revocation,
        )

    def savefile_location(
        self,
        *,
        content_path: str | None = None,
        core_so: str | None = None,
        system: str | None = None,
    ) -> SavefilePlacement | Unresolved:
        """Where this RetroArch install keeps the save for *content_path* under *core_so*.

        *system* is the content's system in ES-DE's vocabulary
        (:func:`atlas.known_systems`). It is what keys a core's recorded file
        set: one core is not one behaviour, so without it the names stay
        unstated unless every system the record covers agrees. Naming it is the
        only way to get file names on an arrangement with no frontend
        catalogue, and it is never guessed from the core — a core's own
        metadata says which systems it *can* run, never which one this content
        is.
        """
        return _retroarch_savefile_location(
            self._machine,
            self._query(content_path=content_path, core_so=core_so, system=system),
        )

    def savestate_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> SavestatePlacement | Unresolved:
        """Where this RetroArch install keeps the savestates for *content_path*.

        A bare install carries the upstream compile-time defaults, and there
        ``sort_savestates_enable`` is **true** (``config.def.h:983``) exactly as
        its savefile twin is — so an unconfigured install sorts states into
        per-``library_name`` subdirectories of ``states`` under the config tree.
        """
        return _retroarch_savestate_location(
            self._machine, self._query(content_path=content_path, core_so=core_so)
        )

    def screenshot_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> ScreenshotPlacement | Unresolved:
        """Where this RetroArch install writes screenshots for *content_path*.

        A bare install carries the upstream compile-time defaults: no
        screenshot directory is configured, so the shots land in the content's
        own directory (task_screenshot.c:547-550), and without content
        RetroArch refuses the shot entirely (:941-943).
        """
        return _retroarch_screenshot_location(
            self._machine, self._query(content_path=content_path, core_so=core_so)
        )

    def texture_pack_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> TexturePlacement | Unresolved:
        """Where this RetroArch install's *core_so* reads texture packs from.

        A bare install wires no shared texture tree, so the answer is the
        emulator's own default location and ``physical_dir`` stays ``None`` —
        which is the honest shape rather than a missing feature: nothing here
        redirects the directory, so there is no second path to report.
        """
        return _retroarch_texture_pack_location(
            self._machine, self._query(content_path=content_path, core_so=core_so)
        )

    def mod_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> ModPlacement | Unresolved:
        """Where this RetroArch install's *core_so* reads mods from.

        A bare install wires no shared mod tree, so each tree's
        ``physical_dir`` stays ``None`` — the honest shape rather than a
        missing feature: nothing here redirects the directories, so there is no
        second path to report.
        """
        return _retroarch_mod_location(
            self._machine, self._query(content_path=content_path, core_so=core_so)
        )

    def soft_patch_candidates(
        self, content_path: str, *, core_so: str | None = None
    ) -> SoftPatchAnswer | Unresolved:
        """Which patch files this RetroArch install would apply to *content_path*.

        A bare install's binary is whatever the user installed, so no packaged
        record covers it and every candidate's ``attempted`` is unestablished —
        which is the same statement the arrangement itself carries about
        everything else it answers.
        """
        return _retroarch_soft_patch_candidates(
            self._machine,
            self._query(content_path=content_path, core_so=core_so),
            build=lookup_soft_patch_build(self.kind),
        )

    def _read_firmware_context(self) -> FirmwareContext:
        # One read of the cfg answers both: its text is the context, its status
        # is the health of an installation whose cfg *is* the marker. The
        # sandbox composes through _cfg_sandbox — on the Flatpak install this
        # context's one read of the app's override files.
        cfg = self._machine.read_text(self._cfg_path())
        sandbox, environment_sources = self._cfg_sandbox()
        return _retroarch_firmware_context(
            sandbox=sandbox,
            global_text=cfg.text,
            cfg_label=RETROARCH_CFG,
            retroarch_config_dir=self.root(),
            findings=self._health_from(cfg.status).issues,
            # A bare install states no version of anything but RetroArch, and
            # this arrangement carries no verified pin to compare one against.
            arrangement_version=None,
            extra_sources=environment_sources,
        )

    # ── The derived catalogue (issue #133) ─────────────────────────────
    # A bare RetroArch ships no frontend catalogue, and the machine still
    # carries an answer: every installed core declares what it is for in the
    # .info beside it. The enumeration is the firmware route's own selection,
    # shared so the two questions can never derive different lists — and the
    # answers say DERIVED on every one, because a catalogue could disagree.

    def _derived_context(self) -> tuple[FirmwareContext, Health]:
        """The core enumeration behind a derived answer, and this query's health.

        One read of the cfg serves both — its text builds the context, its
        status the health — and the context's ``findings`` stay empty on
        purpose: it is consumed for its cores, and the answer states its own
        health rather than fishing it back out of a mixed caveat list.
        """
        cfg = self._machine.read_text(self._cfg_path())
        sandbox, environment_sources = self._cfg_sandbox()
        context = _retroarch_firmware_context(
            sandbox=sandbox,
            global_text=cfg.text,
            cfg_label=RETROARCH_CFG,
            retroarch_config_dir=self.root(),
            findings=(),
            arrangement_version=None,
            extra_sources=environment_sources,
        )
        return context, self._health_from(cfg.status)

    def _systems_answer(self) -> tuple[SystemsAnswer, str | None]:
        context, health = self._derived_context()
        if not context.cores_read:
            return (
                SystemsAnswer(caveats=(*health.issues, self._catalogue_absence())),
                None,
            )
        systems = sorted(
            {core.system for core in context.cores if core.system != "_unknown"}
            | {
                declaration.system
                for core in context.cores
                for declaration in core.firmware
                if declaration.system != "_unknown"
            }
        )
        return (
            SystemsAnswer(
                tuple(systems),
                context.sources,
                (
                    *health.issues,
                    Caveat(
                        CAVEAT_EMULATOR_LIST_DERIVED,
                        "this installation ships no emulator catalogue, so the systems listed "
                        "are those the installed cores' own declarations file under — what a "
                        "catalogue would have declared is unknown",
                        {},
                    ),
                    *(c for c in context.caveats if c.code in _ENUMERATION_CAVEAT_CODES),
                ),
            ),
            None,
        )

    def _platform_view(self) -> tuple[_PlatformView, str | None]:
        """A bare RetroArch's platform reading — every tag vocabulary-backed.

        No frontend catalogue exists here, so no system carries a live
        ``<platform>`` tag and nothing can be a commented block: the declared
        list is the derived one (:meth:`_systems_answer`, issue #133), its
        tags the snapshot column's, all of it named vocabulary-backed. The
        caveats mirror the systems answer's exactly — the derived mark and
        the enumeration statements ride here the same way.
        """
        context, health = self._derived_context()
        if not context.cores_read:
            view = _PlatformView(
                {}, frozenset(), {}, (), (*health.issues, self._catalogue_absence())
            )
            return view, None
        systems = {core.system for core in context.cores if core.system != "_unknown"} | {
            declaration.system
            for core in context.cores
            for declaration in core.firmware
            if declaration.system != "_unknown"
        }
        view = _PlatformView(
            {system: vocabulary_platform_tags(system) or () for system in systems},
            frozenset(systems),
            {},
            context.sources,
            (
                *health.issues,
                Caveat(
                    CAVEAT_EMULATOR_LIST_DERIVED,
                    "this installation ships no emulator catalogue, so the systems listed "
                    "are those the installed cores' own declarations file under — what a "
                    "catalogue would have declared is unknown",
                    {},
                ),
                *(c for c in context.caveats if c.code in _ENUMERATION_CAVEAT_CODES),
            ),
        )
        return view, None

    def _catalogue_answer(
        self, system: str, *, content_path: str | None = None
    ) -> tuple[CatalogueAnswer, str | None]:
        context, health = self._derived_context()
        if not context.cores_read:
            return (
                CatalogueAnswer(caveats=(*health.issues, self._catalogue_absence())),
                None,
            )
        entries, derived = _derived_catalogue_entries(self, context, system)
        return (
            CatalogueAnswer(
                entries,
                context.sources,
                (*health.issues, derived_enumeration_lead(system), *derived),
            ),
            None,
        )

    def _launchable_answer(
        self, system: str, content_path: str
    ) -> tuple[LaunchabilityAnswer, str | None]:
        # The derived entries do not change this refusal: the accept-list is
        # catalogue knowledge, and no core's self-declaration states which
        # files a frontend would scan — the verdict stays a statement about
        # the look.
        cfg_status = self._machine.read_text(self._cfg_path()).status
        return (
            LaunchabilityAnswer(
                verdict=VERDICT_UNKNOWN,
                extension=esde_extension(content_path),
                caveats=(*self._health_from(cfg_status).issues, self._catalogue_absence()),
            ),
            None,
        )

    def entry_savefile_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> SavefilePlacement | Unresolved:
        """The entry route behind a derived entry — the core question, asked by the entry.

        A derived entry is always a libretro core (the enumeration is the
        cores'), so the placement is exactly what the direct question answers
        for that ``core_so``; the guard stands for the day a spec arrives
        from somewhere else.
        """
        if spec.kind != KIND_LIBRETRO:
            return _standalone_savefile_unresolved(spec)
        return _retroarch_savefile_location(
            self._machine,
            self._query(
                content_path=content_path,
                core_so=spec.core_so,
                system=spec.system,
                extra_caveats=entry_caveats,
            ),
        )

    def entry_savestate_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> SavestatePlacement | Unresolved:
        """The savefile entry route's twin — same sources, the savestate keys.

        The guard stands for the day a spec arrives from somewhere else, as on
        the savefile route: a derived entry is always a libretro core.
        """
        if spec.kind != KIND_LIBRETRO:
            return _standalone_savestate_unresolved(spec)
        return _retroarch_savestate_location(
            self._machine,
            self._query(content_path=content_path, core_so=spec.core_so, extra_caveats=entry_caveats),
        )

    def entry_texture_pack_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> TexturePlacement | Unresolved:
        """The texture entry route — the core question, asked by the entry."""
        if spec.kind != KIND_LIBRETRO:
            return _standalone_texture_unresolved(spec)
        return _retroarch_texture_pack_location(
            self._machine,
            self._query(content_path=content_path, core_so=spec.core_so, extra_caveats=entry_caveats),
        )

    def entry_mod_location(
        self,
        spec: EmulatorSpec,
        entry_caveats: tuple[Caveat, ...] = (),
        *,
        content_path: str | None = None,
    ) -> ModPlacement | Unresolved:
        """The mod entry route — the core question, asked by the entry."""
        if spec.kind != KIND_LIBRETRO:
            return _standalone_mod_unresolved(spec)
        return _retroarch_mod_location(
            self._machine,
            self._query(content_path=content_path, core_so=spec.core_so, extra_caveats=entry_caveats),
        )


class BareRetroArchFlatpak(_RetroArchInstall):
    """The ``org.libretro.RetroArch`` Flatpak install."""

    kind = "bare_retroarch_flatpak"
    kinds = ("bare_retroarch_flatpak",)
    _app_id = RETROARCH_FLATPAK_APP_ID

    def __init__(self, home: str, machine: Machine) -> None:
        super().__init__(home, machine, STANDALONE_FLATPAK_CFG_SUFFIX)

    def _cfg_sandbox(self) -> tuple[_Sandbox, tuple[str, ...]]:
        """The Flatpak composition: this app's own override files decide the ``~`` base.

        The same read RetroDECK's handle makes of its own files (issue #101)
        — the bare app's runs are governed by ``org.libretro.RetroArch``'s
        overrides, with the same deploy resolution deciding whether the
        system installation's files speak (:func:`_running_deploy`, the
        resolution its ``/app`` reads already ride).
        """
        return _flatpak_cfg_sandbox(self._machine, self._home, RETROARCH_FLATPAK_APP_ID)


class BareRetroArchNative(_RetroArchInstall):
    """A native ``~/.config/retroarch`` install."""

    kind = "bare_retroarch_native"
    kinds = ("bare_retroarch_native",)

    def __init__(self, home: str, machine: Machine) -> None:
        super().__init__(home, machine, NATIVE_CFG_SUFFIX)


@runtime_checkable
class Installation(Protocol):
    """The surface every installation handle offers — identity, health, placement, catalogue.

    A common protocol instead of a closed union (REVIEW M8): detection returns
    these, and consumers program against the surface. The catalogue question is
    on it because it is a question about an arrangement, not a RetroDECK
    feature: a handle that cannot answer it from a catalogue says *why* in a
    caveat, which is an answer, where an ``isinstance`` narrow left the caller
    to guess. Capabilities that are genuinely one arrangement's — RetroDECK's
    tree roots, its gamelist selections — still live on the concrete handles.
    """

    @property
    def kind(self) -> str: ...

    @property
    def kinds(self) -> tuple[str, ...]: ...

    def root(self) -> str: ...

    def health(self) -> Health: ...

    def savefile_location(
        self,
        *,
        content_path: str | None = None,
        core_so: str | None = None,
        system: str | None = None,
    ) -> SavefilePlacement | Unresolved: ...

    def savestate_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> SavestatePlacement | Unresolved: ...

    def screenshot_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> ScreenshotPlacement | Unresolved: ...

    def texture_pack_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> TexturePlacement | Unresolved: ...

    def mod_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> ModPlacement | Unresolved: ...

    def soft_patch_candidates(
        self, content_path: str, *, core_so: str | None = None
    ) -> SoftPatchAnswer | Unresolved: ...

    def systems(self) -> SystemsAnswer: ...

    def systems_for_platform(self, vocabulary: str, value: str) -> PlatformSystemsAnswer: ...

    def platform_ids(self, system: str) -> SystemPlatformsAnswer: ...

    def emulators_for(self, system: str, *, content_path: str | None = None) -> CatalogueAnswer: ...

    def launchable(self, system: str, content_path: str) -> LaunchabilityAnswer: ...

    def rom_location(self, system: str) -> RomPlacement: ...

    def firmware_for_core(self, core_so: str, *, verify: bool = False) -> FirmwareAnswer: ...

    def firmware_for_system(self, system: str, *, verify: bool = False) -> FirmwareAnswer: ...

    def firmware_inventory(self, *, verify: bool = False) -> FirmwareAnswer: ...

    def identify_firmware(
        self, *, md5: str | None = None, sha1: str | None = None, size: int | None = None
    ) -> FirmwareIdentification: ...
