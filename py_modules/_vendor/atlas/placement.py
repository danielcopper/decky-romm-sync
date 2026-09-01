"""Placements — resolved directories, honest file sets, and provenance.

A :class:`SavefilePlacement` answers "where does this emulator, configured as it is,
keep the save for this content?"; a :class:`SavestatePlacement` answers the same
question for its savestates. Their shape follows the research findings
(``docs/research/retrodeck-save-placement.md`` §16):

- **Directory and file set are different kinds of knowledge.** The directory
  follows from RetroArch's central path rule and is always resolvable; the file
  set is per-core behaviour with no metadata source. For existing saves atlas
  *observes* the set (``glob("<rom_stem>.*")``); otherwise it is honestly
  ``unknown`` — never guessed. An observation in the ROM's own directory says so
  (``content-dir-observation``): there the content shares the name and no source
  tells the two apart. The old fixed ``<rom_stem>.srm`` filename is
  gone: ``.srm`` is only what RetroArch itself writes, and cores like Beetle
  Saturn write ``.bcr``/``.bkr``/``.smpc`` on their own.
- **A hole is not an unknown.** ``needs`` lists holes the caller fills from the
  content at hand (``content_dir``, ``library_name``, ``save_id``); *unknown*
  means atlas cannot state the value and refuses to guess. Distinct states,
  kept distinct. A hole is not confined to the directory: where a rule card
  names the save's files through the content's platform-native id, the file
  set is a template too and the same hole vocabulary carries it.
- **The root varies** — ``savefile_directory`` (explicit, or the RetroArch
  platform default when unset/reset), the system directory (Flycast VMUs), or
  the ROM's own directory (``savefiles_in_content_dir``). Sorting stages apply
  after root selection regardless of which root was chosen
  (``runloop.c:8785-8841``). The system directory is the one *the core is
  handed*, not the cfg key's value: ``systemfiles_in_content_dir`` — or a key
  cleared to nothing — sends the core to the content's own directory
  (``runloop.c:1958-1999``), so a card rooted there answers
  ``content_directory`` on those machines.
- **Filesystem state is part of the answer** — RetroArch silently reverts to the
  unsorted root when a sorted directory cannot be created (``runloop.c:8844``,
  and its savestate twin ``:8878-8887``); ``caveats`` carries that and every
  other stated degradation.

**Why savestates get their own type.** The directory math is shared to the line
— one upstream function resolves both families side by side
(``runloop.c:8752-8979``) — so the resolution is parameterized rather than
copied. What is *not* shared is the field set, and the reason is structural: the
libretro API hands a core no savestate directory at all (``libretro.h`` defines
``GET_SYSTEM_DIRECTORY``, ``GET_SAVE_DIRECTORY``, ``GET_CONTENT_DIRECTORY``,
``GET_PLAYLIST_DIRECTORY`` and ``GET_FILE_BROWSER_START_DIRECTORY``, and nothing
for states), and RetroArch serializes the state itself. A core therefore cannot
deviate from state placement, no rule card for it can ever exist, and
``granularity`` — which is a card's word about how a *core* groups the data it
writes — has an empty domain here. Carrying it as a permanent ``None`` would be
the blank field this grammar refuses, and it could not even be rescued by a
caveat: there is nothing to report. So :class:`SavestatePlacement` is
:class:`SavefilePlacement` minus that one field, the way :class:`RomPlacement`
already carries the fields its own question has and no others.

Pure compute. No I/O — the installation handles observe the machine and pass
the results in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Literal, Mapping, TypeVar

from .retroarch_cfg import RetroArchCfg

# Root kinds — where the placement's directory is anchored. The closed
# vocabularies are Literal types so an invalid state is a type error first
# and a constructor error second (REVIEW M10). Each question has its own set,
# because a vocabulary is closed only if it is closed around one question: a
# savefile is never anchored at savestate_directory, and a savestate is never
# anchored at a core's system directory (no card can move it — see the module
# docstring), so a shared union would hand every client values its own branch
# can never see.
RootKind = Literal[
    "savefile_directory",
    "content_directory",
    "system_directory",
    "working_directory",
    "emulator_directory",
]
StateRootKind = Literal[
    "savestate_directory", "content_directory", "emulator_directory", "working_directory"
]
FileSetState = Literal["observed", "declared", "unknown"]

# The three states a file set can be in, as values. Every other closed
# vocabulary in the contract ships both its type and its constants; a client
# branches on `file_set.state`, and a literal in that branch is a typo away
# from silently never matching.
FILE_SET_OBSERVED: FileSetState = "observed"
FILE_SET_DECLARED: FileSetState = "declared"
FILE_SET_UNKNOWN: FileSetState = "unknown"

ROOT_SAVEFILE_DIRECTORY: RootKind = "savefile_directory"
ROOT_CONTENT_DIRECTORY: RootKind = "content_directory"
ROOT_SYSTEM_DIRECTORY: RootKind = "system_directory"
# The root that is a property of the launch rather than of the machine: the
# working directory of the process that loads the core. DeSmuME 2015 composes
# its .dsv path from a variable its libretro build never fills, so the file
# lands relative to wherever RetroArch was started — knowable to whoever did
# the starting, which is why the answer is a ``<cwd>`` template with a hole
# rather than a refusal: a hole is something the caller fills, and the caller
# often IS the launcher.
ROOT_WORKING_DIRECTORY: RootKind = "working_directory"
# The root a standalone emulator owns: its user tree below the XDG base the
# arrangement pins. No frontend hands these emulators a save directory — the
# tree is the emulator's own, its shape read from the emulator's configuration
# the way the emulator reads it, and where an arrangement routes pieces of it
# elsewhere it does so with symlinks the answer walks, not with settings.
ROOT_EMULATOR_DIRECTORY: RootKind = "emulator_directory"

# The screenshot question's own two-word vocabulary, closed around its own
# question like the savestate one is: a screenshot is never anchored at a
# save root or a core's system directory — RetroArch writes it itself, into
# its screenshot directory or the content's own (task_screenshot.c:488-550
# at a79435a).
ScreenshotRootKind = Literal["screenshot_directory", "content_directory"]
SCREENSHOT_ROOT_DIRECTORY: ScreenshotRootKind = "screenshot_directory"
SCREENSHOT_ROOT_CONTENT_DIRECTORY: ScreenshotRootKind = "content_directory"
SCREENSHOT_ROOT_KINDS = ("screenshot_directory", "content_directory")

ROOT_SAVESTATE_DIRECTORY: StateRootKind = "savestate_directory"
# The content root is the one value both questions share, and it is the same
# fact on both: the ROM's own directory, reached either by the family's
# in-content-dir flag or by a root that resolved to nothing.
STATE_ROOT_CONTENT_DIRECTORY: StateRootKind = "content_directory"
# The savefile family's words, joined here when the standalone savestate cards
# landed (#225), and they are the same facts on both questions: the tree a
# standalone emulator owns below the XDG base the arrangement pins, and the
# working directory of the launching process, which is where melonDS anchors a
# relative SavestatePath exactly as it anchors a relative SaveFilePath.
# RetroArch's four cfg keys never anchor at either — only a standalone entry's
# answer does — so the vocabulary stays closed around the question: a savestate
# is still never anchored at a save root or a core's system directory.
STATE_ROOT_EMULATOR_DIRECTORY: StateRootKind = "emulator_directory"
STATE_ROOT_WORKING_DIRECTORY: StateRootKind = "working_directory"

ROOT_KINDS = (
    "savefile_directory",
    "content_directory",
    "system_directory",
    "working_directory",
    "emulator_directory",
)
STATE_ROOT_KINDS = (
    "savestate_directory",
    "content_directory",
    "emulator_directory",
    "working_directory",
)
_FILE_SET_STATES = ("observed", "declared", "unknown")

# How a texture pack tree is keyed below its root — the values
# :attr:`TexturePlacement.keying` may take, and therefore the values a texture
# card may state. World knowledge with the same standing as a granularity: no
# read of the machine recovers it, so it is stated only where a citation backs
# it and left absent otherwise. Closed like every other vocabulary here, and
# named per value so a client branches on a constant rather than a literal.
KEYING_GAME_ID = "game-id"
KEYING_SERIAL = "serial"
KEYING_TITLE_ID = "title-id"
KEYING_ROM_NAME = "rom-name"
KEYING_PACK = "pack"
# The game's *title* rather than any identifier of it: the name an emulator's
# own database gives the disc, which a user may have overridden in that
# emulator's list. Distinct from ``rom-name`` on purpose — the content file's
# name is one of the things such a title falls back to, never the thing it is —
# and a caller filling it reads it off the emulator, not off the content path.
KEYING_TITLE = "title"
KEYINGS = (
    KEYING_GAME_ID,
    KEYING_SERIAL,
    KEYING_TITLE_ID,
    KEYING_ROM_NAME,
    KEYING_PACK,
    KEYING_TITLE,
)
Keying = Literal["game-id", "serial", "title-id", "rom-name", "pack", "title"]

# The patch formats RetroArch looks for beside the content, **in the order it
# tries them** (``task_patch.c:1071-1075`` at RetroArch a79435a). The order is
# compiled in, not configured, which is why it is a constant here rather than a
# card's word: nothing on a machine can reorder it. Closed like every other
# vocabulary in this module, and named per value so a client branches on a
# constant.
PATCH_FORMAT_IPS = "ips"
PATCH_FORMAT_BPS = "bps"
PATCH_FORMAT_UPS = "ups"
PATCH_FORMAT_XDELTA = "xdelta"
PATCH_FORMATS = (PATCH_FORMAT_IPS, PATCH_FORMAT_BPS, PATCH_FORMAT_UPS, PATCH_FORMAT_XDELTA)
PatchFormat = Literal["ips", "bps", "ups", "xdelta"]

# The indexed continuations a patch chain may carry: one digit appended to the
# whole file name, 1 through 9 (``task_patch.c:1121-1147``). Nine and not more
# is the upstream implementation's own bound — it writes a single character into
# the byte behind the name and says so in a comment — so the list a candidate
# carries is exactly as long as the emulator's own loop.
PATCH_CONTINUATION_INDICES = tuple(range(1, 10))

# How a save is grouped — the values :attr:`Granularity.value` may take, and
# therefore the values a rule card may select. Contractual: clients branch on
# them and vectors assert them, so the packaged cards are validated against
# this tuple at load rather than against a card author's spelling. Named per
# value like every other closed set here, and the tuple is built from the names
# so the vocabulary has one source rather than two that can drift.
GRANULARITY_SHARED_CARD = "shared-card"
GRANULARITY_SHARED_FILE = "shared-file"
GRANULARITY_PER_GAME_FILE = "per-game-file"
GRANULARITY_PER_GAME_FILES = "per-game-files"
# One *directory* per game, and the directory is the unit: everything below it
# belongs to that one game, so a sync client packs and moves the tree whole
# rather than tracking files inside it. Distinct from per-game-files, which
# says several of this game's files sit in a directory *shared* with other
# games' — there the file is the unit and the directory is not safe to move.
GRANULARITY_PER_GAME_DIRECTORY = "per-game-directory"
GRANULARITIES = (
    GRANULARITY_SHARED_CARD,
    GRANULARITY_SHARED_FILE,
    GRANULARITY_PER_GAME_FILE,
    GRANULARITY_PER_GAME_FILES,
    GRANULARITY_PER_GAME_DIRECTORY,
)
# The one value outside the tuple above, deliberately: it is what
# :attr:`Granularity.value` says for a mode that keeps no save data at all
# (write protection on — the writes are discarded), so no *group* may ever
# carry it — a group is a place save data lives, and this value says none
# does. The card loader validates groups against GRANULARITIES and never
# sees this word; the contract's granularity.value vocabulary includes it.
GRANULARITY_NONE = "none"

# What a group of files *is*, as against whom it belongs to. The two questions
# are deliberately separate fields: a granularity says whether a file is this
# game's or every game's, and a role says what kind of data it holds, so the
# same fact is never spelled twice and the two can never contradict each other.
# MAME writes its per-machine dip switches to ``<machine>.cfg`` and its
# emulator-wide input defaults to ``default.cfg`` in one directory — same role,
# different granularity — and that pair is why neither field can carry the
# other's meaning.
#
# Closed and named per value like every other vocabulary here. A client syncing
# save data takes every role but :data:`ROLE_SETTINGS`; see ``docs/how-to-use.md``.
#
# :data:`ROLE_HIGH_SCORE` is the arcade family's, and it is a separate value
# rather than a battery because the *merge* differs, which is the one thing a
# role exists to tell a client. Two devices that both played a game hold two
# saves and the newer one wins; they hold two score tables and neither wins —
# the higher entries do. A machine keeps one table for everyone who ever played
# it, so it is not one player's progress the way a battery save is. Folding it
# into :data:`ROLE_BATTERY` would state something true (it is save data, back it
# up) while losing the only part a client could act on.
# :data:`ROLE_NOTES` is the user's own words about a game — MAME 2010 keeps
# the debugger's per-machine comment files under its save tree — and it is a
# separate value for the same reason the score table is: what a client does
# with it. Notes are neither progress a newer copy overwrites nor settings a
# save sync skips; a client lifts them deliberately (into RomM's per-game
# notes, say) or leaves them, and either needs the word to decide by.
ROLE_BATTERY = "battery"
ROLE_MEMORY_CARD = "memory-card"
ROLE_DISK_DIFF = "disk-diff"
ROLE_HIGH_SCORE = "high-score"
ROLE_SETTINGS = "settings"
ROLE_NOTES = "notes"
ROLES = (
    ROLE_BATTERY,
    ROLE_MEMORY_CARD,
    ROLE_DISK_DIFF,
    ROLE_HIGH_SCORE,
    ROLE_SETTINGS,
    ROLE_NOTES,
)


_FrozenValue = TypeVar("_FrozenValue")


def _freeze(mapping: Mapping[str, _FrozenValue]) -> Mapping[str, _FrozenValue]:
    """A read-only copy — frozen dataclasses stay deeply immutable."""
    return MappingProxyType(dict(mapping))

# Caveat codes — the stable, machine-readable identifiers clients branch on.
# Part of the API contract; messages are for humans and may change freely.
CAVEAT_NO_CORE = "no-core"
CAVEAT_CORE_UNQUERYABLE = "core-unqueryable"
CAVEAT_SORTED_DIR_MISSING = "sorted-dir-missing"
# No "health" code lives here: an installation's health findings are caveats
# already, with their own stable codes (atlas.installations.HEALTH_ISSUE_*), and
# they ride in an answer's caveat list as themselves. A category code wrapping
# them would put the real condition in data for a client to unpack.
CAVEAT_FILENAMES_UNVERIFIED = "filenames-unverified"
CAVEAT_FILENAMES_CONTENT_CONDITIONAL = "filenames-content-conditional"
CAVEAT_FILE_SET_SPANS_ROOTS = "file-set-spans-roots"
# A directory this configuration writes save data into whose file names do not
# follow from anything atlas reads. MAME's differencing images for CHD hard
# disks are the case it was added for: the name is the disk image's own, taken
# from the machine's ROM table inside the binary, and upstream says on the line
# that builds it that the scheme "doesn't scale". The directory is named because
# it is knowable and because a backup that skips it loses the player's progress
# on every machine with a hard disk; the names are not, because guessing them
# would be the failure this project exists to avoid. `data` carries `dir` and a
# `citation` for the reading behind it.
CAVEAT_FILE_NAMES_UNESTABLISHED = "file-names-unestablished"
# The caller named no system, and the answer holds anyway because every system
# the core's record covers writes the same files. The claim is scoped to those
# systems — `data["systems"]` lists them — because a core run for a system its
# record never names has established nothing here. It is the honest answer on
# an arrangement with no frontend catalogue, where no system is ever named.
CAVEAT_FILE_SET_ACROSS_SYSTEMS = "file-set-across-systems"
CAVEAT_UNKNOWN_OPTION_VALUE = "unknown-option-value"
# Set to blank or the literal "default". Not the same as absent — an absent
# key resolves to RetroArch's platform default on every route — and the
# distinction is the whole reason this code is spelled 'cleared': the setting
# is present and empty, and what a core is handed then depends on the run.
CAVEAT_SYSTEM_DIRECTORY_CLEARED = "system-directory-cleared"
CAVEAT_PER_GAME_OVERRIDES_PRESENT = "per-game-overrides-present"
# The frontend's statement, split off the code above (#311): some games of this
# system select a different emulator in their gamelist entry, so a system-level
# entry order may be wrong for exactly those games. Its own code because a
# caveat code is the thing a client switches on — the two facts were riding one
# code told apart only by the shape of ``data``, and both can ride one answer
# at once. ``per-game-overrides-present`` stays the emulators' statement (a
# settings file layered over the very answer it rides, with ``core``, ``dir``
# and ``key``); this one carries the total ``count`` and ``emulators``, the
# selected emulator label mapped to how many games select it.
CAVEAT_PER_GAME_ALTERNATIVE_EMULATOR = "per-game-alternative-emulator"
CAVEAT_PER_GAME_OVERRIDE = "per-game-override"
# The third state those two leave out: an emulator that layers per-game files
# over its whole configuration (PCSX2 installs one game's
# ``<DataRoot>/gamesettings/<serial>_<crc>.ini`` while it runs — a sibling of
# the ``inis`` directory its global settings live in, not a child of it) and a
# directory holding them that could not be read. Neither sibling can say it — "present"
# asserts they exist and their absence is what tells a caller the answer holds
# for every game, so silence here claims more than was established. A code of
# its own rather than one of theirs with the real condition hidden in ``data``:
# the two facts a client acts on differently are "some games override this" and
# "whether any does was not checked". ``data`` carries the directory and the
# key the layer could have changed.
CAVEAT_PER_GAME_LAYER_UNREAD = "per-game-layer-unread"
# The fourth state, and the one Dolphin's shape needed: a per-game layer whose
# DIRECTORY no read of the machine finds, because the emulator compiles it in.
# Dolphin loads two such layers — the user's ``GameSettings/`` and a second one
# below the build's own ``Sys`` tree, and the second lives at the compile-time
# ``DATA_DIR "sys/"`` that is written nowhere on a running machine.
# ``per-game-layer-unread`` is the wrong code for it: that one says a listing
# atlas COULD make did not succeed, so a client can act — fix the permission,
# read the directory itself. This one says no listing was ever possible from
# here, so there is nothing to retry and no count will ever appear. Keeping
# them apart is the whole point: one is a degraded check, the other is a
# structural limit, and a client that treats them alike either retries forever
# or gives up on a directory it could have read. ``data`` carries the keys the
# layer could move and which layer it is (``layer``); it carries NO ``dir``,
# and that absence is the honest half — atlas cannot spell this directory.
CAVEAT_PER_GAME_BUILD_LAYER_UNREAD = "per-game-build-layer-unread"
CAVEAT_UNVERIFIED_VERSION = "unverified-version"
# The frontend writes this core no save file — established from its source, not
# guessed — and what the core writes on its own is a different question nobody
# has answered yet. Both halves matter, which is why the empty file set does not
# travel alone: read on its own, "no files" would tell a client syncing saves
# that a Nintendo DS game has nothing to back up, when DeSmuME fills no libretro
# memory id and still keeps its saves somewhere. So the emptiness is stated as
# what it is — a fact about the FRONTEND — and this says the other half is open.
CAVEAT_CORE_OWN_WRITES_UNESTABLISHED = "core-own-writes-unestablished"
CAVEAT_INVALID_SAVE_DIRECTORY = "invalid-save-directory"
# The screenshot family's own spelling of the refused-directory fact, because
# the consequence differs: a refused save root falls back to the value that
# stood before it or the platform default, while a refused screenshot
# directory is simply cleared at config load (configuration.c:6733-6741) and
# the shots land in the content's own directory. One code per consequence, so
# a client never has to guess which family's fallback follows.
CAVEAT_INVALID_SCREENSHOT_DIRECTORY = "invalid-screenshot-directory"
CAVEAT_CORE_SUSPECT = "core-suspect"
CAVEAT_CORE_UNAUDITED = "core-unaudited"
CAVEAT_CORE_MULTI_OPTION = "core-multi-option"
CAVEAT_CORE_GENERATION_MISMATCH = "core-generation-mismatch"
# The installed core could not be read at all, so which generation is on this
# machine was never established and the recorded deviation is not applied. Its
# own code, and never together with the mismatch above: that one is a core that
# WAS read and answered for a generation the record does not describe. Here
# nothing answered, which is a different thing to tell a client.
CAVEAT_CORE_GENERATION_UNESTABLISHED = "core-generation-unestablished"
# The core was read and the recorded deviation fits it, but which value governs
# the option it hangs on was never established: no configuration on this machine
# states one, and the core did not state its default either. One level below the
# code above — there the generation is unknown, here the generation is fine and
# the *setting* is unknown — and exclusive with both of the two above it, since
# each of them has already retired the card before an option can be read.
CAVEAT_CORE_OPTION_VALUE_UNESTABLISHED = "core-option-value-unestablished"
# The governing options file carries an entry for a key this card's core
# generation retired — an older generation wrote it, the rename or split left
# it behind, and RetroArch never prunes the file, so the value someone set
# there silently stopped applying (issue #79). The inverse direction of
# core-generation-mismatch: there the RECORD names a key the core does not
# register and the card steps aside; here the FILE names one and the answer
# stands on the current key, with this caveat naming the dead entry, its
# value, and the file a user would prune. Stated only for keys the card
# records as retired, each with the binary evidence — attributing an
# arbitrary unregistered entry to a core would be guessing, and the cores
# that register their options too late for any probe (LRPS2) could never be
# swept generically at all.
CAVEAT_OPTION_ENTRY_RETIRED = "option-entry-retired"
CAVEAT_SORTED_DIR_UNCREATABLE = "sorted-dir-uncreatable"
# The resolved save root exists on the host and the app cannot touch it: a
# flatpak override file revokes the filesystem access the app's own metadata
# grants (a '!'-prefixed entry hides the tree as a tmpfs even under a broader
# grant — flatpak 1.16.6, flatpak-context.c:1096-1108 via the tmpfs branch,
# flatpak-exports.c:340-378 longest-covering-entry-wins — or a negated special
# token drops the grant wholesale). The answer stands, because the config
# really names this directory; the caveat is the statement that writes there
# will not land where the answer points (issue #103).
CAVEAT_SAVE_ROOT_REVOKED = "save-root-revoked"
CAVEAT_DEAD_SYMLINK = "dead-symlink"
CAVEAT_SYMLINK_LOOP = "symlink-loop"
CAVEAT_SAVE_DIR_UNLISTABLE = "save-dir-unlistable"
# The working_directory root's rider: the directory is a property of how the
# emulator is launched, not of anything on disk, so no read of the machine can
# resolve it — the answer stays a template whose one hole only the launcher
# can fill.
CAVEAT_SAVE_DIR_LAUNCH_DEPENDENT = "save-dir-launch-dependent"
# No separate save file exists: the loaded content file itself takes the
# writes. The file set is a declared emptiness — true as stated — and this
# caveat is what keeps it from reading as "this game has no save". A client
# decides for itself what to make of a content file that doubles as the save
# (back it up, copy it, leave it); atlas states the fact and stops there.
CAVEAT_SAVE_INSIDE_CONTENT = "save-inside-content"
# The inside-content statement's console-shaped sibling: the saves live inside
# a disk image the answer names as a file — xemu's emulated Xbox hard disk
# holds every game's save on its FATX filesystem. Per-game structure exists
# *inside* the image (``data`` carries the layout, one directory per title id)
# but nothing outside it is addressable, so a file-level client backs the
# image up whole or leaves it, and a tool that parses the image's filesystem
# has the layout stated machine-readably instead of rediscovering it.
CAVEAT_SAVE_INSIDE_IMAGE = "save-inside-image"
# The save-inside-image statement, one question over: the savestates live
# inside a disk image the answer names as a file. xemu's snapshots are QEMU
# internal snapshots — the VM state is written into the qcow2 hard-disk image
# and the snapshot record spans every snapshot-capable device, of which that
# image is the only one, and xemu's own snapshot browser reads them back out
# of the file hdd_path names. Nothing outside the image is addressable per
# snapshot — there is no file per state — so a file-level client backs the
# image up whole, and ``data`` carries the image and the cited naming of the
# entries inside it so a qcow2-aware tool has the layout stated
# machine-readably instead of rediscovering it.
CAVEAT_SAVESTATE_INSIDE_IMAGE = "savestate-inside-image"
# The inside-content statement's harder sibling: this configuration keeps no
# save at all — the writes are discarded (hatari with write protection on
# throws the modified image away at eject). The declared emptiness is the
# whole truth here, and the caveat is what separates "nothing is kept" from
# both "no separate file" above and "nobody looked". The granularity block
# still travels, with value "none": the option readings and alternatives in
# it are how a caller sees which switch to flip to make saves exist again.
CAVEAT_SAVE_WRITES_DISCARDED = "save-writes-discarded"
# A rule card fits this core, but which of its recorded modes is in force
# could not be established — the selection rule needed something this
# question did not carry (hatari's write-protect story splits on whether the
# content is a floppy or a hard-disk image, so without a content path there
# is no class to select with) or something the machine does not state. The
# recorded behaviour is not applied; ``data`` carries the core and the
# reason. Sibling of core-option-value-unestablished, one level up: there
# the option's value is missing, here the rule as a whole cannot decide.
CAVEAT_CORE_MODE_UNESTABLISHED = "core-mode-unestablished"
# The emulator's own configuration routes its saves to a directory outside
# every root kind this answer format states — ScummVM's scummvm.ini can set
# 'savepath' to any directory at all. The path is read off the machine and
# carried in ``data`` (with the file it came from), because a client that
# skips it loses the saves; the placement itself falls back to the standard
# answer rather than stating a root kind that would be a lie.
CAVEAT_SAVE_ROOT_REDIRECTED = "save-root-redirected"
# The redirected caveat's harder sibling: the emulator anchors its save trees
# in the frontend *process's* state — its working directory, its environment —
# and that is a value no read of this machine can establish (MAME with its own
# paths in force resolves ``states/mame/nvram`` against whatever directory
# RetroArch happens to run from). Where redirected states the directory it
# read, this one states the relative trees that would hang off the
# unknowable anchor, in ``data``, so a caller sees what to look for once it
# knows the anchor atlas refuses to guess. The placement itself falls back to
# the standard answer, marked as where the frontend would look.
CAVEAT_SAVE_ROOT_UNRESOLVABLE = "save-root-unresolvable"
CAVEAT_SANDBOX_PATH_UNTRANSLATED = "sandbox-path-untranslated"
CAVEAT_APP_RELATIVE_PATH_UNEXPANDED = "app-relative-path-unexpanded"
CAVEAT_CFG_LINE_DROPPED = "cfg-line-dropped"
CAVEAT_CFG_VALUE_REJECTED = "cfg-value-rejected"
CAVEAT_CONTENT_DIR_OBSERVATION = "content-dir-observation"
CAVEAT_CONTENT_PATH_UNNAMED = "content-path-unnamed"
# The core's own .info says it cannot make savestates. A statement about the
# declaration, not about the directory: the placement still resolves, and the
# caveat is what keeps "here is the directory" from reading as "and states will
# appear in it".
CAVEAT_CORE_SAVESTATES_UNSUPPORTED = "core-savestates-unsupported"
# The declaration caveat's standalone sibling, per machine instead of per
# core: MAME flags save-state support driver by driver (MACHINE_SUPPORTS_SAVE,
# gamedrv.h:76 at mame0287), the flags are compiled into the binary, and no
# read of this machine can say which side the launched system falls on. An
# unflagged system still WRITES the state file — MAME saves it and warns
# "Save states are not officially supported for this system"
# (machine.cpp:927-928) — so the placement stands, and this caveat is what
# keeps "here is the directory" from reading as "and every machine's states
# are reliable". ``data`` carries the emulator and the citation.
CAVEAT_SAVESTATE_SUPPORT_MACHINE_DEPENDENT = "savestate-support-machine-dependent"
# A directory is stated, and that this emulator reads it is not established.
# The texture family's own degradation, and one level weaker than every other
# code here: those qualify an answer atlas resolved, this one qualifies the
# premise underneath it. Three libretro cores port a standalone emulator and
# build their tree under a user directory whose root nobody has watched them
# choose — the same open question the audit already carries for their saves —
# so the location is the one the reading derives and the caveat says nobody has
# seen it read. Never to be read as "the packs go nowhere": the directory is
# real and stated; what is open is whether the emulator looks there.
CAVEAT_EMULATOR_READ_UNESTABLISHED = "emulator-read-unestablished"
# The directory is stated and the switch beside it is not, because the setting
# that would answer it lives in a configuration file of the emulator's own that
# atlas does not read. The texture family's second degradation, and the one a
# standalone emulator's answer always carries today: where a libretro core's
# option is read through RetroArch's own options chain, a standalone emulator
# keeps its settings in its own ini, and modelling those is a different piece of
# work (issue #3). Distinct from a value nobody stated — there the file was read
# and said nothing; here no file was read at all.
CAVEAT_EMULATOR_CONFIG_UNREAD = "emulator-config-unread"
# The feature is off and this build offers no way to turn it on — not a core
# option, not a setting any config file reaches. The strongest of the three
# statements this family makes about a switch, and the only one that is about
# the *build* rather than about a value: ``enabled`` is ``false`` here as an
# established fact, not as a reading of some file. It can never ride with
# ``emulator-read-unestablished``, which says the opposite kind of thing — there
# the read path is in doubt, here it is established and simply never taken.
CAVEAT_FEATURE_SWITCH_ABSENT = "feature-switch-absent"
# Which patch formats the RetroArch on this machine attempts is not established.
# The soft-patching question's own degradation, and the one place in atlas where
# a *frontend* build's compile-time flags decide an answer: patching as a whole
# and the ``.xdelta`` applier in particular are ``HAVE_PATCH`` / ``HAVE_XDELTA``
# (``Makefile.common:260-267``), and nothing a running machine writes down says
# which way they were set — no setting, no log, no file. So the candidate paths
# are stated (they follow from the content path alone) and each one's
# ``attempted`` is left unanswered rather than assumed from a build default.
# Never to be read as "patching is off here": what is missing is the reading,
# not the feature.
CAVEAT_PATCH_FORMATS_UNESTABLISHED = "patch-formats-unestablished"
# This emulator has a mod directory *and* the frontend patches its content
# before it ever gets there. Stated on the mod answer because the two mechanisms
# are easy to mistake for one: a caller holding an IPS file for an FBNeo romset
# has two true places to put it — the core's own ``ips`` tree, and beside the
# ROM where RetroArch itself would apply it — and they behave differently (one
# is a core feature under a core option, the other is the frontend patching a
# buffer). Not a degradation of the directory answer: everything it states
# stands, and this points at the other question.
CAVEAT_SOFT_PATCHING_APPLIES = "soft-patching-applies"


@dataclass(frozen=True, slots=True)
class Caveat:
    """A stated degradation — structured, so clients can act on it.

    ``code`` is a stable identifier from the ``CAVEAT_*`` constants and part of
    the API contract: clients branch on it, vectors assert it. ``message`` is
    the human-readable explanation and may change freely. ``data`` carries the
    machine-readable specifics (e.g. the fallback directory of a silent
    revert) as a read-only mapping. Decision-relevant → structured;
    explanatory → text.
    """

    code: str
    message: str
    # Values are strings, except the mapping-valued key the alternative-emulator
    # code documents beside its constant (``emulators``) — read-only mappings
    # here, plain objects in the serialized contract.
    data: Mapping[str, "str | Mapping[str, str]"] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("Caveat: code must be a non-empty stable identifier")
        object.__setattr__(
            self,
            "data",
            _freeze(
                {
                    key: _freeze(value) if isinstance(value, Mapping) else value
                    for key, value in self.data.items()
                }
            ),
        )

# The holes a ``needs`` list may carry, each one a value the CALLER fills from
# the content at hand. Contractual: clients read them, vectors assert them.
# Nothing a config states belongs here — a configured value no caller can
# supply is atlas's own to resolve or to state as a degradation, never a
# template handed over as if it were fillable.
HOLE_CONTENT_DIR = "content_dir"
HOLE_LIBRARY_NAME = "library_name"
HOLE_SAVE_ID = "save_id"
# The two the resolver fills itself whenever the caller names content, so they
# reach ``needs`` only on a content-less question: the content file's own stem,
# and the basename of the directory it lies in. Two holes rather than one,
# because they are different facts about the same path and a caller fills each
# by itself — exactly the split between ``<rom_stem>`` in a declared file name
# and the ``content_dir`` the sorted root keeps.
HOLE_ROM_STEM = "rom_stem"
HOLE_CONTENT_DIR_NAME = "content_dir_name"
# The one hole no content can fill: the working directory of the process that
# will load the core. It exists for the ``working_directory`` root and is
# genuinely the caller's to fill — a frontend knows what cwd it launches with.
HOLE_CWD = "cwd"
# The hole a region-keyed answer keeps: which of an emulator's per-region
# trees this game's save lands in is decided by the disc's own region field,
# which atlas does not read out of content — but a caller with metadata (a
# library server knows its games' regions) fills it and narrows the answer to
# one tree. The groups list every tree either way, so a caller without the
# region still sees the whole candidate set.
HOLE_REGION = "region"

# The template tokens a rule card's declared file names may carry, and the hole
# each leaves behind. ``<rom_stem>`` the resolver fills itself from the content
# path; ``<save_id>`` it never can — a core that names the save after the
# content's platform-native id (Flycast's per-game VMUs are the disc's product
# number, ``oslib.cpp:44-52`` at flycast@1dac369) reads that id out of the ROM
# itself, which atlas does not do: identifying content is not locating a save.
# So the id stays a hole for whoever knows it, exactly like ``content_dir``.
TEMPLATE_ROM_STEM = "<rom_stem>"
TEMPLATE_SAVE_ID = "<save_id>"
# A card's *subdir* may template a whole segment on the content, because two
# read cores key the directory itself that way: prboom creates
# ``<save_dir>/<rom_stem>/`` (libretro.c:2633), the vitaquake2 family
# ``<save_dir>/<basename of the content's directory>/`` (libretro.c:2259-2262).
# A token must be the whole segment — ``_base_of`` undoes a subdir by counting
# segments, and that stays exact only while one template fills to exactly one.
TEMPLATE_CONTENT_DIR_NAME = "<content_dir_name>"
# The whole-directory template the content-rooted answers keep when no content
# was named — the spelling `needs`'s content_dir hole fills.
TEMPLATE_CONTENT_DIR = "<content_dir>"
# The whole-directory template a working-directory-rooted answer keeps — the
# spelling `needs`'s cwd hole fills, and never a directory atlas read.
TEMPLATE_CWD = "<cwd>"
# The segment template a region-keyed emulator tree keeps until a caller fills
# the ``region`` hole — one path segment, exactly like the subdir templates,
# because that is the granularity Dolphin swaps it at (GetGCIFolderPath
# replaces the last segment, MainSettings.cpp:844-873 at 2603a).
TEMPLATE_REGION = "<region>"
_FILE_NAME_HOLES: Mapping[str, str] = MappingProxyType({TEMPLATE_SAVE_ID: HOLE_SAVE_ID})
SUBDIR_TEMPLATE_HOLES: Mapping[str, str] = MappingProxyType(
    {TEMPLATE_ROM_STEM: HOLE_ROM_STEM, TEMPLATE_CONTENT_DIR_NAME: HOLE_CONTENT_DIR_NAME}
)


def _holes(named: list[str]) -> tuple[str, ...]:
    """The distinct holes of a template, in the order they first appear.

    One template can name the same hole twice — ``savefiles_in_content_dir``
    roots at the content directory and ``sort_savefiles_by_content_enable``
    appends its name again (``runloop.c:8789`` then ``:8827``), so the directory
    really is ``<content_dir>/<content_dir>``. The caller still fills one value,
    and ``needs`` is the list of things to fill, not of positions to substitute
    (REVIEW L4).
    """
    return tuple(dict.fromkeys(named))


def file_set_holes(files: Iterable[str]) -> tuple[str, ...]:
    """The holes a declared file-set template still carries, in template order.

    A card may name a save's files through a fact only the content carries.
    Those names are stated as they are — the template is the answer — and the
    hole travels to ``needs`` so a caller sees what is left to fill instead of
    reading a literal ``<save_id>`` off a resolved-looking name.
    """
    return _holes([hole for name in files for token, hole in _FILE_NAME_HOLES.items() if token in name])


def needs_with_file_set(needs: Iterable[str], files: Iterable[str]) -> tuple[str, ...]:
    """Every hole of an answer: the directory template's, then the file names'."""
    return _holes([*needs, *file_set_holes(files)])


@dataclass(frozen=True, slots=True)
class FileGroup:
    """One directory's worth of a save, with what it is and whom it belongs to.

    A save is not always one list of files in one place. MAME keeps a machine's
    battery memory under ``nvram/``, its dip switches under ``cfg/`` beside an
    emulator-wide ``default.cfg``, and disk write-differences under ``diff/``;
    FinalBurn Neo puts a per-game ``.fs`` and a ``shared.memcard`` in one
    directory. Those are different data with different owners, and a single
    ``(dir, files, granularity)`` triple has to lie about at least one of them.

    Each group carries its own resolved ``dir`` — groups may sit under different
    roots — its own ``granularity`` and its own ``role``. What a group never
    carries is prose: what a file is *for* beyond its role belongs in the
    answer's ``sources``, not in a field a client would have to parse.

    ``files`` is ``None`` where the emulator writes save data into this directory
    under names that follow from nothing atlas reads — MAME names a hard disk's
    differencing image after the disk's entry in the machine's own ROM table, and
    its memory cards after an index chosen in the emulator's own interface.
    ``None`` and ``()`` are different claims and the difference is the point:
    ``()`` would say *this directory holds nothing*, which is exactly what such a
    group does not say. Read it as "there is save data here and I cannot list
    it": a backup takes the directory whole, and a name-based sync knows it is
    blind there instead of silently skipping the player's progress. The reason
    travels beside the answer as ``file-names-unestablished``, which carries the
    citation; the group is where the directory itself belongs, so that one walk
    over ``groups`` reaches every place a save lives.
    """

    dir: str
    files: tuple[str, ...] | None
    granularity: str
    role: str

    def __post_init__(self) -> None:
        if self.granularity not in GRANULARITIES:
            raise ValueError(
                f"FileGroup: granularity must be one of {GRANULARITIES}, got {self.granularity!r}"
            )
        if self.role not in ROLES:
            raise ValueError(f"FileGroup: role must be one of {ROLES}, got {self.role!r}")
        if self.files is not None and not self.files:
            raise ValueError(
                "FileGroup: a group with no files is not a group — a directory whose names are "
                "not established carries None, which is a different claim"
            )


@dataclass(frozen=True, slots=True)
class FileSet:
    """The files a save consists of — observed, declared, or unknown.

    ``state`` is ``"observed"`` (``files`` are real basenames found on disk),
    ``"declared"`` (``files`` come from a verified rule card — world knowledge
    with cited provenance, not a guess), or ``"unknown"`` (``files`` is empty;
    atlas refuses to guess). ``provenance`` says how the state was reached.

    A declared set can itself be a template: where the card names the files
    through the content's own id, the names keep their ``<save_id>`` hole and
    :data:`SavefilePlacement.needs` lists it. Stating the shape in full is not the
    same as claiming the resolved name — it is the directory grammar applied to
    file names.

    *Observed* means a snapshot of matching files currently seen — it never
    implies the whole save. ``complete`` is the explicit completeness claim:
    ``True`` only when a source-verified rule card closes the candidate
    universe for the active mode; the generic observation can never earn it.

    **Today the field is reserved: it is ``False`` on every answer atlas can
    give**, because no shipped rule card claims completeness and none can yet.
    Closing the candidate universe means establishing which files the core can
    write *at all* for the active mode — an upstream read, not an inventory of
    what a card happens to list — and no card's evidence goes that far. The
    field stays in the contract at its honest value rather than being dropped:
    a client must not read "not complete" as "atlas has no opinion", and the
    audit grind can earn a ``True`` here one card at a time without the shape
    of an answer changing.

    ``groups`` decomposes a declared set whose parts differ in kind or owner —
    see :class:`FileGroup`. It is empty wherever nothing decomposed the answer,
    which is every observation, every unknown and every standard-rule
    declaration; empty means *not decomposed*, never *no files*.

    Where it is populated, ``files`` stays exactly what it always was — the
    names lying in ``dir`` — and that is enforced: it must be every group under
    the first group's directory whose names are established, in order. So a
    client that never reads ``groups`` sees no change when a card splits one
    list into two by role, while a client that does gets the parts under the
    other directories too. Cards state the save's own state first, so the first
    group is the one a save-syncing client would have taken anyway.

    **``groups`` is the complete list of places, and ``files`` is one of them.**
    A group whose names are not established carries ``files=None`` and still
    appears here, so a single walk over ``groups`` reaches every directory the
    card knows about — there is no second structure to correlate. A group with
    ``None`` contributes nothing to ``files``, which is what keeps the flat list
    exactly the set of names a caller can look for.
    """

    state: FileSetState
    files: tuple[str, ...]
    provenance: str
    complete: bool = False
    groups: tuple[FileGroup, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in _FILE_SET_STATES:
            raise ValueError(f"FileSet: state must be one of {_FILE_SET_STATES}, got {self.state!r}")
        if self.state == "unknown" and (self.files or self.complete):
            raise ValueError("FileSet: an unknown set carries no files and no completeness claim")
        if self.groups and self.state != FILE_SET_DECLARED:
            raise ValueError("FileSet: only a declared set is decomposed into groups")
        if self.groups:
            here = tuple(
                name
                for group in self.groups
                if group.dir == self.groups[0].dir and group.files is not None
                for name in group.files
            )
            if here != self.files:
                raise ValueError(
                    "FileSet: files must be every group in the answer's own directory whose names "
                    f"are established, in order — got {self.files}, groups say {here}"
                )


@dataclass(frozen=True, slots=True)
class OptionReading:
    """One switch that decides the granularity, read live: key, value, and where.

    ``value`` is the live value the resolver read — or ``None`` where the
    setting has no entry anywhere and its effect falls to a default the
    ``provenance`` explains. ``provenance`` is prose (which file stated it, or
    the core's registered default); ``options_file`` is where a caller would
    change it — change it, ask again, and the new answer confirms the switch.
    """

    key: str
    value: str | None
    provenance: str
    options_file: str | None


@dataclass(frozen=True, slots=True)
class ModeAlternative:
    """One other mode a caller could switch to, and the settings that select it.

    ``options`` is the combination that reaches the mode — for a single-option
    core one pair, for a rule card one pair per switch. ``values`` is every
    distinct grouping among that mode's groups, in card order with the mode's
    own first: one entry for most modes, and the honest plural for a mixed one
    (FinalBurn Neo's shared mode writes a per-game save beside a card every
    game shares, and a single value would hide the shared file — the
    understatement issue #128 was about). A client that wants one word reads
    ``values[0]``, which is exactly what the old single value said.
    """

    mode: str
    options: tuple[tuple[str, str], ...]
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Granularity:
    """How this emulator, configured as it is, groups save data — and how to change it.

    ``value`` is the current granularity (``"shared-card"``,
    ``"per-game-file"``, …, or ``"none"`` for a mode that keeps no save at
    all); ``mode`` names the rule-card mode in force — an option value where
    one option governs, the card's own mode name where a rule selects, and
    ``None`` where no card speaks (the standard rule's per-game grouping).
    ``readings`` is one :class:`OptionReading` per switch that went into the
    selection — one for a single-option card, several for a rule card, none
    where nothing selects. ``alternatives`` lists the other reachable modes
    as :class:`ModeAlternative`, each with the option combination that
    selects it. A core with fixed behaviour (e.g. LRPS2) carries no readings
    and no alternatives. ``provenance`` is prose: how the mode came to be
    selected — for a granularity with readings each reading carries its own,
    and this sentence is the summary; for one without, it is the whole story
    (the standard rule's per-content keying, a card's fixed behaviour).
    """

    value: str
    mode: str | None
    readings: tuple[OptionReading, ...]
    alternatives: tuple[ModeAlternative, ...]
    provenance: str


UNKNOWN_FILE_SET = FileSet(
    state=FILE_SET_UNKNOWN,
    files=(),
    provenance="file set not stated — no observation available (never guessed)",
)


@dataclass(frozen=True, slots=True)
class SavefilePlacement:
    """A resolved save location with provenance and stated degradations.

    ``dir`` is concrete when the caller supplied the content path; otherwise it
    is a template whose remaining holes are listed in ``needs`` — as are the
    holes a declared file-set template keeps, so ``needs`` is the answer's
    holes, not the directory's alone. ``root_kind``
    names the anchor (:data:`ROOT_SAVEFILE_DIRECTORY`,
    :data:`ROOT_CONTENT_DIRECTORY`, :data:`ROOT_SYSTEM_DIRECTORY`,
    :data:`ROOT_WORKING_DIRECTORY` — the launch's own directory, always a
    ``<cwd>`` template with its hole in ``needs`` — or
    :data:`ROOT_EMULATOR_DIRECTORY`, a standalone emulator's own tree).
    ``file_set`` is observed or unknown, never guessed. ``sources`` is the
    provenance trail; ``caveats`` states every degradation explicitly.

    ``granularity`` is ``None`` wherever no rule card states it. That alone
    does not separate "nothing to report" from "atlas deliberately does not
    state this", so the separation is a caveat, not an empty field: a core
    whose granularity depends on options atlas does not interpret carries
    :data:`CAVEAT_CORE_MULTI_OPTION` naming those options.

    A placement can be *conditional*: when ``dir`` does not exist yet,
    RetroArch attempts to create it on first save and silently reverts to the
    unsorted root when creation fails — ``fallback_dir`` names that root, so
    the two possible outcomes are structural, not prose (REVIEW H5).
    ``physical_dir`` is the fully link-resolved backing directory when ``dir``
    reaches its files through symlinks (RetroDECK's ``dir_prep`` pattern) —
    the emulator-side path and the physical path are two truthful answers to
    different questions (REVIEW M7); a dead link is a ``dead-symlink`` caveat
    instead.
    """

    dir: str
    root_kind: RootKind
    needs: tuple[str, ...]
    file_set: FileSet
    sources: tuple[str, ...]
    caveats: tuple[Caveat, ...]
    granularity: Granularity | None = None
    fallback_dir: str | None = None
    physical_dir: str | None = None

    def __post_init__(self) -> None:
        if not self.dir:
            raise ValueError("SavefilePlacement: dir must be non-empty (an unanswerable placement is Unresolved)")
        if self.root_kind not in ROOT_KINDS:
            raise ValueError(f"SavefilePlacement: root_kind must be one of {ROOT_KINDS}, got {self.root_kind!r}")


@dataclass(frozen=True, slots=True)
class ScreenshotPlacement:
    """Where RetroArch writes this configuration's screenshots (issue #142).

    The savefile placement's shape minus the fields whose domain is empty
    here, each omission the contract rather than an oversight. No
    ``file_set``: the default naming is the content's stem plus a timestamp
    (``fill_str_dated_filename``, task_screenshot.c:517-535 at a79435a), so
    no closed set of names exists to declare or observe. No ``fallback_dir``:
    RetroArch creates the directory at the moment of the shot and simply
    fails the shot when it cannot (task_screenshot.c:553-556) — there is no
    revert-to-unsorted the way the save families have. No ``granularity``:
    nothing groups screenshots but the directory itself.

    ``root_kind`` speaks the question's own two-word vocabulary. The
    ``content_directory`` root is reached three ways, all stated in
    ``sources``: the ``screenshots_in_content_dir`` flag (which outranks even
    a configured directory, task_screenshot.c:547-550), a key that is unset
    or reset to ``"default"``, and a configured directory that does not exist
    — RetroArch clears that at config load rather than creating it
    (configuration.c:6733-6741), which the ``invalid-screenshot-directory``
    caveat carries machine-readably.
    """

    dir: str
    root_kind: ScreenshotRootKind
    needs: tuple[str, ...]
    sources: tuple[str, ...]
    caveats: tuple[Caveat, ...]
    physical_dir: str | None = None

    def __post_init__(self) -> None:
        if not self.dir:
            raise ValueError("ScreenshotPlacement: dir must be non-empty")
        if self.root_kind not in SCREENSHOT_ROOT_KINDS:
            raise ValueError(
                f"ScreenshotPlacement: root_kind must be one of {SCREENSHOT_ROOT_KINDS}, "
                f"got {self.root_kind!r}"
            )


# Unresolved outcome codes — stable identifiers like caveat codes.
UNRESOLVED_STANDALONE = "standalone-unsupported"
# The emulator is known and carded, but the binary THIS entry would launch is
# not the one whose configuration is established: an arrangement's launcher
# script picks at run time (EmuDeck's cemu.sh probes an AppImage, then a
# flatpak, then a Windows build under Proton), and only an established
# variant's config answers. A statement about atlas's wiring, never about the
# emulator; ``data`` names the variant so a client can say why this launch
# has no read placement. Distinct from ``standalone-unsupported`` (no card at
# all) and from ``emulator-config-unreadable`` (the established config exists
# and the machine would not let it be read).
UNRESOLVED_STANDALONE_VARIANT_UNESTABLISHED = "standalone-variant-unestablished"
# The caller named a core this installation does not have, and the cores
# directory was read well enough to establish that. One fact, one code on both
# routes: the firmware route says it with a caveat
# (``atlas.firmware.CAVEAT_CORE_NOT_INSTALLED``, which is this same string), the
# save routes with this outcome, and a client that learned the word on one route
# reads the other. Not to be confused with a core that is *there* and will not
# load — that one still has a placement, with its generation left unestablished.
UNRESOLVED_CORE_NOT_INSTALLED = "core-not-installed"
# Nothing establishes where this emulator reads texture packs, so no directory
# is named. A statement about atlas, never about the emulator: it does NOT say
# the emulator has no texture-pack feature, and a client rendering it that way
# reports something nobody checked. Most cores are simply outside the packaged
# wiring knowledge; one — LRPS2 at its shipped generation — is deliberately left
# out of it, because the only path anyone can name for it is one an arrangement
# builds rather than one the emulator was seen to read.
UNRESOLVED_TEXTURE_WIRING_UNESTABLISHED = "texture-wiring-unestablished"
# The texture refusal's twin, one family over: nothing establishes where this
# emulator reads mods, so no directory is named. A statement about atlas and
# never about the emulator — most emulators are simply outside the packaged
# wiring, and one is deliberately outside it: MAME's plugin directories are
# values an installer writes into ``mame.ini``, so naming them would state an
# arrangement's directory as an emulator's read location.
UNRESOLVED_MOD_WIRING_UNESTABLISHED = "mod-wiring-unestablished"
# A standalone emulator's save answer hangs entirely on a configuration of the
# emulator's own, and that file exists and could not be read. There is no
# standard frame to fall back to the way a libretro card steps aside — the
# frontend hands these emulators nothing — so the whole question refuses,
# with the file named in ``data``. Distinct from ``standalone-unsupported``:
# there atlas has no wiring for the emulator, here it does and the machine
# would not let it look.
UNRESOLVED_EMULATOR_CONFIG_UNREADABLE = "emulator-config-unreadable"
# The emulator's configuration was read, and the absolute path it states has
# no spelling on this host from here: the value names a location only the
# emulator's sandbox can read, and the whole answer hangs on that one path,
# so there is nothing to anchor at. The caveat vocabulary states the same
# fact as a degradation (``sandbox-path-untranslated``) where an answer still
# stands around it; this is that fact as a refusal, with the untranslatable
# value in ``data["path"]`` beside the file that states it. Distinct from
# ``emulator-config-unreadable``: there the machine would not let the file be
# read, here the read succeeded and the stated value is what cannot be
# reached. ``data["path"]`` is always the primary stated value; an aggregate
# refusal covering several stated files (xemu's save answer, where the disk
# image and the EEPROM ride one question) additionally carries
# ``data["paths"]`` — every untranslatable value, in the emitter's stated
# order (for xemu: the disk image first, then the EEPROM) — whenever it
# names more than one.
UNRESOLVED_EMULATOR_CONFIG_PATH_UNTRANSLATABLE = "emulator-config-path-untranslatable"


@dataclass(frozen=True, slots=True)
class Unresolved:
    """A question atlas cannot answer for this entry — a domain outcome, not an error.

    Returned where an answer route exists but the subject is outside the
    resolver's current coverage (e.g. a standalone emulator entry before the
    standalone block lands). ``code`` is a stable identifier clients branch
    on; ``message`` says why; ``data`` carries the specifics. Callers switch
    on the result type — nothing raises at runtime (REVIEW M8).
    """

    code: str
    message: str
    # Values are strings, except the list-valued keys an aggregate refusal
    # documents beside its code (``paths`` on the untranslatable-path code) —
    # tuples here, lists in the serialized contract.
    data: Mapping[str, "str | tuple[str, ...]"] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("Unresolved: code must be a non-empty stable identifier")
        object.__setattr__(self, "data", _freeze(self.data))


@dataclass(frozen=True, slots=True)
class SavestatePlacement:
    """A resolved savestate location with provenance and stated degradations.

    :class:`SavefilePlacement` field for field, minus ``granularity`` — see the
    module docstring for why that one cannot exist here. ``dir`` is concrete
    when the caller supplied the content path; otherwise it is a template whose
    remaining holes are listed in ``needs``. ``root_kind`` names the anchor
    (:data:`ROOT_SAVESTATE_DIRECTORY`, :data:`STATE_ROOT_CONTENT_DIRECTORY`,
    or — for a standalone entry's answer, never for RetroArch's —
    :data:`STATE_ROOT_EMULATOR_DIRECTORY` and
    :data:`STATE_ROOT_WORKING_DIRECTORY`).

    ``file_set`` is where the two questions differ in substance rather than in
    fields. A savefile's set is per-core behaviour with no metadata source; a
    savestate's is RetroArch's own, and atlas can state it: the base name is
    ``<stem>.state`` (``runloop.c:8942-8949``, ``file_path_special.h:44``), the
    numbered slots are ``<stem>.state<N>`` for N above zero and the auto slot is
    ``<stem>.state.auto`` (``runloop.c:8185-8207``). Which of them exist is
    still an observation — the slot is a live setting and nothing on disk says
    how many were ever written — so the set is never ``complete``.

    ``fallback_dir`` and ``physical_dir`` mean exactly what they do on a save
    placement: the root RetroArch silently reverts to when it cannot create the
    sorted directory (``runloop.c:8878-8887``), and the link-resolved backing
    directory where the answer is reached through symlinks.
    """

    dir: str
    root_kind: StateRootKind
    needs: tuple[str, ...]
    file_set: FileSet
    sources: tuple[str, ...]
    caveats: tuple[Caveat, ...]
    fallback_dir: str | None = None
    physical_dir: str | None = None

    def __post_init__(self) -> None:
        if not self.dir:
            raise ValueError("SavestatePlacement: dir must be non-empty")
        if self.root_kind not in STATE_ROOT_KINDS:
            raise ValueError(
                f"SavestatePlacement: root_kind must be one of {STATE_ROOT_KINDS}, got {self.root_kind!r}"
            )


@dataclass(frozen=True, slots=True)
class SavestateAbsence:
    """This emulator has no savestates — a stated, cited fact, and an answer.

    The savestate question's third shape (#284), beside the placement and the
    refusal, because "the feature does not exist" is neither: a refusal says
    atlas cannot answer, a placement would have to invent a directory that is
    not there, and this says the answer is *nowhere* — Cemu 2.6 ships no state
    serializer, the Ryujinx lineage never had one. ``citation`` is the
    evidence for the no (the spans and scans that establish it, contractual —
    a client repeating the claim repeats its source); ``sources`` is the
    card's provenance prose, non-contractual like every provenance.

    ``caveats`` carries what qualifies the *claim*, never what qualifies a
    tree: the card's own ``unverified-version`` where no shipped build pins
    the claim (nothing ships Ryubing; the record reads a release); the
    arrangement's evidence caveats — a stated no is world knowledge pinned to
    the build an arrangement was verified with, so an arrangement atlas never
    observed, or one observed on another version, says so here exactly as it
    does on a placement; and the entry's catalogue-status and per-game
    override caveats — a gamelist that would launch a DIFFERENT emulator for
    this game is a statement about emulator identity, and "this emulator has
    no savestates" needs the rider that this emulator may not be what runs.
    Tree-derived caveats (health findings, link walks) do not ride: the
    absence names no path for them to qualify.
    """

    emulator: str
    citation: str
    sources: tuple[str, ...]
    caveats: tuple[Caveat, ...] = ()

    def __post_init__(self) -> None:
        if not self.emulator:
            raise ValueError("SavestateAbsence: emulator must be non-empty")
        if not self.citation:
            raise ValueError(
                "SavestateAbsence: citation must be non-empty — a stated no without its "
                "evidence is indistinguishable from a guess"
            )


@dataclass(frozen=True, slots=True)
class TexturePlacement:
    """Where this emulator, configured as it is, reads texture packs from.

    The third placement, and the first whose *root* is not RetroArch's to
    choose: a core that supports texture replacement builds the tree itself,
    under a directory it derives from one of the roots RetroArch hands it. So
    the split runs the other way round from the save family — the root is read
    live (the system directory as the core receives it, or the save root as it
    stands), the fragment below it is per-core behaviour no config states, and
    the two are joined here.

    ``dir`` is concrete when every hole is filled and otherwise a template whose
    remaining holes are listed in ``needs`` — the same grammar
    :class:`SavefilePlacement` answers with, and for the same reason: a core
    rooted in the system directory is handed the *content's* directory wherever
    ``systemfiles_in_content_dir`` sends it, so a caller who named no content
    gets ``<content_dir>`` and a hole to fill rather than a directory nobody
    picked. ``physical_dir`` is the link-resolved backing directory where the
    answer is reached through symlinks — the shape every distribution that wires
    a shared texture tree into place produces — and a traversal that ends
    nowhere is a ``dead-symlink`` or ``symlink-loop`` caveat instead.

    Two fields are this question's own, and each is a different kind of
    knowledge:

    - ``enabled`` says whether replacement is switched on **right now**, read
      from the option that governs it: the options file that RetroArch would
      read first, else the default the installed core itself registers. It is
      ``None`` where neither answered — the option is not in any file atlas
      could read *and* the core did not state a default (or registered a value
      outside the ones the card knows). A directory whose feature is off is
      still the right directory, which is exactly why the two facts are separate
      fields rather than one hedged answer.
    - ``keying`` names how the tree below ``dir`` is divided per game
      (:data:`KEYINGS`) and is ``None`` wherever no cited evidence states it.
      It is world knowledge — nothing on the machine spells it — so it follows
      the boundary rule to the letter: cited or absent, never derived into an
      answer.
    """

    dir: str
    needs: tuple[str, ...]
    enabled: bool | None
    keying: Keying | None
    sources: tuple[str, ...]
    caveats: tuple[Caveat, ...]
    physical_dir: str | None = None

    def __post_init__(self) -> None:
        if not self.dir:
            raise ValueError(
                "TexturePlacement: dir must be non-empty (an unanswerable placement is Unresolved)"
            )
        if self.keying is not None and self.keying not in KEYINGS:
            raise ValueError(f"TexturePlacement: keying must be one of {KEYINGS}, got {self.keying!r}")


@dataclass(frozen=True, slots=True)
class ModTree:
    """One directory a mod goes into, and how the tree below it is keyed.

    ``dir`` and ``physical_dir`` mean exactly what they mean on a
    :class:`TexturePlacement`: the path the emulator opens, and the
    link-resolved directory the bytes are really in where an arrangement wired
    one. ``keying`` follows the same cited-or-absent rule.

    ``role`` is this family's own field, and its domain is the reason the
    family's answer is plural at all: an emulator may read mods from **several**
    directories that are not alternatives to each other but different mechanisms
    — FBNeo takes a replacement romset from ``patched``, an IPS patch set from
    ``ips`` and a romdata file from ``romdata``, all governed by one switch. The
    role is the emulator's own word for such a tree, so a caller with three
    directories in hand can tell which is which. It is ``None`` where the
    emulator reads mods from one directory, because there is then nothing to
    tell apart — a made-up name for the only tree would be vocabulary a client
    has to learn to ignore.
    """

    dir: str
    keying: Keying | None
    role: str | None = None
    physical_dir: str | None = None

    def __post_init__(self) -> None:
        if not self.dir:
            raise ValueError("ModTree: dir must be non-empty")
        if self.keying is not None and self.keying not in KEYINGS:
            raise ValueError(f"ModTree: keying must be one of {KEYINGS}, got {self.keying!r}")


@dataclass(frozen=True, slots=True)
class ModPlacement:
    """Where this emulator, configured as it is, reads mods from.

    The texture family's answer with one difference, and the difference is the
    plural: ``trees`` replaces the single ``dir``/``keying``/``physical_dir``
    trio, because a mod is not one kind of thing. Ten of the eleven rows atlas
    ships carry exactly one tree; FBNeo carries three, each a different
    mechanism under one switch, and an answer that named one of them would be
    two-thirds wrong for a caller who has an IPS patch in hand.

    That plurality is deliberate rather than incidental, and it is the shape
    this grammar will need again: a single question can have several true
    locations at once, which is the same class of answer the save side runs into
    where one core writes to two roots for one game (issue #97).

    Everything else is the texture answer, field for field and meaning for
    meaning. ``needs`` are the holes of the *answer* — a root resolved into the
    content's own directory leaves the same hole for every tree hanging off it,
    so the list belongs here rather than on each tree. ``enabled`` is the switch
    that governs the feature, read live where a live read exists, ``None``
    where nothing established it and never to be read as *off*.
    """

    trees: tuple[ModTree, ...]
    needs: tuple[str, ...]
    enabled: bool | None
    sources: tuple[str, ...]
    caveats: tuple[Caveat, ...]

    def __post_init__(self) -> None:
        if not self.trees:
            raise ValueError(
                "ModPlacement: at least one tree (an emulator with no directory is Unresolved)"
            )
        roles = [tree.role for tree in self.trees]
        if len(self.trees) > 1:
            if None in roles:
                raise ValueError(
                    "ModPlacement: every tree of a multi-tree answer names its role — that is what "
                    "the field is for"
                )
            if len(set(roles)) != len(roles):
                raise ValueError(f"ModPlacement: roles must tell the trees apart, got {roles}")


@dataclass(frozen=True, slots=True)
class SoftPatchCandidate:
    """One patch file RetroArch would look for beside the content, and its chain.

    ``path`` is the absolute file name the frontend composes: the content's own
    basename with its last extension stripped, plus this format's extension
    (``runloop.c:5196-5253``, over the basename ``runloop.c:8673-8713`` built).
    ``continuations`` are the indexed follow-ups applied on top of it once it
    hits — the same name with one digit appended, 1 through 9, stopping at the
    first gap (``task_patch.c:1121-1147``). They are listed rather than
    described because they *are* file names: a rule for composing them would
    hand a client the one piece of upstream arithmetic this answer exists to
    have done for it.

    ``attempted`` says whether the RetroArch on this machine tries this format
    at all, and it is ``None`` wherever nobody established that — the flags are
    compile-time (:data:`CAVEAT_PATCH_FORMATS_UNESTABLISHED`) and no read of a
    running machine recovers them. ``None`` is not *no*: a patch in an
    unestablished format may well apply, and a client that renders it as
    unsupported reports something nobody checked.
    """

    format: PatchFormat
    path: str
    continuations: tuple[str, ...]
    attempted: bool | None = None

    def __post_init__(self) -> None:
        if self.format not in PATCH_FORMATS:
            raise ValueError(
                f"SoftPatchCandidate: format must be one of {PATCH_FORMATS}, got {self.format!r}"
            )
        if not self.path:
            raise ValueError("SoftPatchCandidate: path must be non-empty")


@dataclass(frozen=True, slots=True)
class SoftPatchAnswer:
    """Which files RetroArch would patch this content with, before any core sees it.

    The family's second question, and the only one whose subject is the
    **content** rather than an emulator: soft patching is the frontend's own,
    it has no directory of its own — the files sit beside the ROM — and no
    configuration key anywhere governs it (a grep for ``soft_patching`` over
    the pinned tree finds none; the one patch-related setting,
    ``notification_show_patch_applied``, governs an on-screen message). What
    could change it is a command line — ``--ips=``/``--bps=``/``--ups=``/
    ``--xdelta=`` force one format, ``--no-patch`` blocks patching entirely
    (``retroarch.c:7458-7465``, consumed ``task_content.c:695-696``,
    ``:1203-1204``) — and no launcher on either arrangement passes one.

    So this answer is exact where the rest of the family hedges: ``candidates``
    is the whole candidate set in attempt order, derived from the content path
    by arithmetic atlas ports rather than from anything it had to look up.

    ``applies`` is the one live per-core reading: RetroArch patches only content
    it loads **into memory**, which is every core that does not need a full path
    (``task_content.c:744-745`` sets the flag from the core's own
    ``retro_system_info.need_fullpath``, ``:1736-1737`` puts it on the content
    element, ``:1469`` skips the memory load — and with it the patch call at
    ``:1195-1215`` — when it is set). ``True`` means this core loads into memory
    and is therefore patched, ``False`` means it never is, ``None`` means atlas
    could not establish it: no core was named, or the core's ``.info`` states no
    ``needs_fullpath``, or it could not be read (which says so in a caveat).

    Two further gates ride every ``True`` and are stated in prose rather than in
    fields, because neither is a fact about this machine: only the **first**
    content file is patched (``idx == 0``), and only when it is not a media type
    (``first_content_type == RARCH_CONTENT_NONE``, ``task_content.c:1195-1198``).
    A single-file launch — what every launcher on both arrangements performs —
    meets both.

    **Nothing here is written to disk.** The patch is applied to the in-memory
    buffer the core is handed (``task_patch.c:872-879``); the ROM beside the
    patch file is not modified, and no answer of atlas's is a statement that it
    will be.
    """

    candidates: tuple[SoftPatchCandidate, ...]
    applies: bool | None
    sources: tuple[str, ...]
    caveats: tuple[Caveat, ...]


def build_soft_patch_candidates(
    *, content_basename: str, attempted: Mapping[str, bool] | None = None
) -> tuple[SoftPatchCandidate, ...]:
    """The four candidates for a content basename, in RetroArch's attempt order.

    *content_basename* is ``runtime_content_path_basename`` — the value
    :func:`atlas.content_path.content_basename` ports, which is already the
    archive-aware, extension-truncated name, so content inside an archive is
    answered with the inner file's name in the archive's own directory. An
    empty one names no file and yields no candidates: appending ``.ips`` to
    nothing would name a dotfile in a directory nobody asked about.

    *attempted* maps a format to whether this build tries it; a format the
    mapping does not carry is left ``None``, which is the honest state wherever
    no record establishes the build.
    """
    if not content_basename:
        return ()
    states = attempted or {}
    return tuple(
        SoftPatchCandidate(
            format=fmt,
            path=f"{content_basename}.{fmt}",
            continuations=tuple(
                f"{content_basename}.{fmt}{index}" for index in PATCH_CONTINUATION_INDICES
            ),
            attempted=states.get(fmt),
        )
        for fmt in PATCH_FORMATS
    )


@dataclass(frozen=True, slots=True)
class _ResolvedDirectory:
    """RetroArch's path math applied to one family's layout — the shared part."""

    dir: str
    rooted_in_content: bool
    needs: tuple[str, ...]
    sources: tuple[str, ...]


def _resolve_placement_dir(
    *,
    layout: RetroArchCfg,
    platform_default_dir: str,
    content_dir_path: str | None,
    content_dir_name: str | None,
    library_name: str | None,
) -> _ResolvedDirectory:
    """The directory both families are placed by — one port of one upstream rule.

    Root selection first (``runloop.c:8785-8813``), then the sorting stages,
    which run regardless of how the root was selected (``runloop.c:8822-8888``)
    — content component first, then ``library_name``. The savefile and
    savestate halves of that function are the same shape line for line; the
    only thing that differs is which four settings were read, which is what
    ``layout.keys`` carries.
    """
    needs: list[str] = []
    sources = list(layout.sources)
    keys = layout.keys

    if layout.in_content_dir:
        rooted_in_content = True
        sources.append(f"layout: root is the ROM's own directory ({keys.in_content_dir})")
        if content_dir_path is not None:
            parts = [content_dir_path]
        else:
            parts = [TEMPLATE_CONTENT_DIR]
            needs.append(HOLE_CONTENT_DIR)
    else:
        rooted_in_content = False
        parts = [platform_default_dir if layout.directory is None else layout.directory]

    if layout.sort_by_content:
        if content_dir_name is not None:
            parts.append(content_dir_name)
        else:
            parts.append(TEMPLATE_CONTENT_DIR)
            needs.append(HOLE_CONTENT_DIR)
    if layout.sort_by_core:
        if library_name is not None:
            parts.append(library_name)
        else:
            parts.append("<library_name>")
            needs.append(HOLE_LIBRARY_NAME)

    return _ResolvedDirectory(
        dir=os.path.join(*parts),
        rooted_in_content=rooted_in_content,
        needs=_holes(needs),
        sources=tuple(sources),
    )


def build_savefile_placement(
    *,
    layout: RetroArchCfg,
    platform_default_dir: str,
    content_dir_path: str | None,
    content_dir_name: str | None,
    library_name: str | None,
    extra_sources: tuple[str, ...] = (),
    caveats: tuple[Caveat, ...] = (),
    file_set: FileSet = UNKNOWN_FILE_SET,
) -> SavefilePlacement:
    """Compose a :class:`SavefilePlacement` from a resolved layout and the caller's fills.

    ``platform_default_dir`` is the arrangement's RetroArch platform default
    saves directory (``saves`` under the config tree, ``platform_unix.c:2133-2134``)
    — the effective root whenever ``savefile_directory`` is unset or reset.
    ``content_dir_path`` / ``content_dir_name`` derive from the content path
    when the caller supplied one (the ROM's own directory and its basename);
    when absent the corresponding hole is left in the template and listed in
    ``needs``. ``library_name`` is the core's self-reported name (via
    ``query_core``); when the layout sorts by core and it is absent, the
    ``<library_name>`` hole remains.
    """
    resolved = _resolve_placement_dir(
        layout=layout,
        platform_default_dir=platform_default_dir,
        content_dir_path=content_dir_path,
        content_dir_name=content_dir_name,
        library_name=library_name,
    )
    return SavefilePlacement(
        dir=resolved.dir,
        root_kind=ROOT_CONTENT_DIRECTORY if resolved.rooted_in_content else ROOT_SAVEFILE_DIRECTORY,
        needs=resolved.needs,
        file_set=file_set,
        sources=(*resolved.sources, *extra_sources),
        caveats=tuple(caveats),
    )


def build_savestate_placement(
    *,
    layout: RetroArchCfg,
    platform_default_dir: str,
    content_dir_path: str | None,
    content_dir_name: str | None,
    library_name: str | None,
    extra_sources: tuple[str, ...] = (),
    caveats: tuple[Caveat, ...] = (),
    file_set: FileSet = UNKNOWN_FILE_SET,
) -> SavestatePlacement:
    """The savestate twin of :func:`build_savefile_placement`, over the same path math.

    ``platform_default_dir`` is ``states`` under the config tree
    (``platform_unix.c:2135-2136``) — the effective root whenever
    ``savestate_directory`` is unset or reset, the same way its savefile twin
    resolves to ``saves``.
    """
    resolved = _resolve_placement_dir(
        layout=layout,
        platform_default_dir=platform_default_dir,
        content_dir_path=content_dir_path,
        content_dir_name=content_dir_name,
        library_name=library_name,
    )
    return SavestatePlacement(
        dir=resolved.dir,
        root_kind=(
            STATE_ROOT_CONTENT_DIRECTORY if resolved.rooted_in_content else ROOT_SAVESTATE_DIRECTORY
        ),
        needs=resolved.needs,
        file_set=file_set,
        sources=(*resolved.sources, *extra_sources),
        caveats=tuple(caveats),
    )
