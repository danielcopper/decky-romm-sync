# An adopted install is an install, deletion authority included — the proof sits at adoption time

## Status

Accepted. Records the decisions behind [#260](https://github.com/danielcopper/decky-romm-sync/issues/260) (adopt an
existing ROM file instead of re-downloading), which [#1680](https://github.com/danielcopper/decky-romm-sync/issues/1680)
surfaced from the field: a new install with ROMs copied in by hand, where the game detail offered Download and never
Play.

## Context

"Installed" is a `rom_installs` row and nothing else — `installed = install is not None` (`services/game_detail.py`).
The plugin does not scan the ROM directories, so a file the user placed there themselves has no record, and the Play
button follows the record. Reported as a permissions problem, because the manually placed files were `0755` and a plugin
download lands at `0644`; nothing in the plugin reads the file mode.

Downloading then destroys the file. `services/downloads.py` has no existence check on the ROM target path, and the
finalize move is a plain `os.replace` (`adapters/download_file.py`). The multi-file path is worse than a replace:
`make_dirs` is `exist_ok=True` and the extract runs into whatever directory is already there, so same-named files are
overwritten, the user's other files survive, and the merged directory is then recorded as `rom_dir`. An uninstall
`remove_tree`s it whole — including the files that were never part of the ROM. The only existing guard is the ES-DE
collapse rename, which skips on collision and logs a warning the user never sees.

This is the case the invariant register's _"never delete data that exists nowhere else"_ describes, and the usual
justification for its confirm leg does not reach it. Removed-game cleanup deletes installed ROM content on confirmation
because _"the ROM is re-downloadable from RomM where a save is not"_. Here the whole premise is that the file is the
user's own — a different rip, a translation patch, a romhack — which is precisely what the server cannot hand back.

What makes adoption decidable is that RomM already carries the evidence. `RomFileSchema` exposes `file_size_bytes`,
`crc_hash`, `md5_hash`, `sha1_hash` and `is_top_level` per file, and `RomArchiveMember` carries `name`, `size` and the
three digests per member **non-optionally**, so the server knows the content inside its own archives. Hashes are
computed by default and opt out through `filesystem.skip_hash_calculation`.

Cost was measured rather than assumed. On a Steam Deck SD card the read rate is 77 MiB/s, so a 1.53 GiB image hashes in
19.8 s — I/O-bound, identical for CRC32, MD5 and SHA-1. Across a real library the median ROM is 2 MiB, roughly 70 % sit
under 10 MiB, and about 11 % exceed 500 MiB. A ZIP's central directory yields each member's uncompressed size and CRC32
without decompressing anything; 10 of 24 top-level ROM files in that library are `.zip`, so the archived case is the
common one, not the exception. Platform directories hold one or two subdirectories, while a single multi-file install
can hold tens of thousands of files — 53 864 in the largest observed — which is why any directory scan must stay on the
top level.

## Decision

**An adopted install is a `rom_installs` row like any other, deletion authority included.** Uninstall and removed-game
cleanup delete an adopted ROM's files exactly as they delete a downloaded ROM's. There is no provenance column and no
downstream exemption.

**The protection therefore sits at the moment of adoption**, in a dialog the user cannot skip. It opens whenever
something is in the way:

| Trigger                                                                               | Answer                           |
| ------------------------------------------------------------------------------------- | -------------------------------- |
| A target path is occupied (multi-file has two: the extract dir and its collapse name) | Ask                              |
| A file or directory elsewhere in the platform directory matches the server's sizes    | Ask, and offer it as a candidate |
| Neither                                                                               | Download                         |

**Cheap evidence decides whether to ask; content verification is always a button, never a wait imposed before the
dialog.** Sizes for a file; the top-level name set plus sizes for a directory, matched against `is_top_level`, with
extra files allowed — the plugin's own directories carry a generated `.m3u` and a healed `PS3_DISC.SFB` that the
server's manifest does not list. Where the content is archived the digests come from the central directory at no cost. A
server without hashes turns the candidate search **off** rather than lowering its bar.

**The dialog has three exits.** Adopt records the existing content as the install. Download replaces it, behind a second
confirmation naming the deletion. Cancel does nothing. For multi-file, Download removes the existing directory and
extracts fresh — the user chose replace, not merge.

**The game detail page runs one `stat`** on the computed target path, and only when no install row exists, so a ROM
already sitting in place is not offered as an undifferentiated Download. The full answer stays at click time.

## Consequences

**The plugin deletes files it did not create, and this is deliberate.** It is the direct consequence of having one class
of install row. The alternative is not "safer by default" — it is a second branch in uninstall, removed-game cleanup,
RetroDECK-home migration and version switch, each a place where a future change forgets the distinction. One row shape
with a hard gate in front of it is auditable; two row shapes with a soft gate are not.

**Nothing is set aside on the destructive exit.** The user is choosing between two named outcomes after being shown what
is there and offered a content check, which is a stronger confirm than the register's own precedent. A quarantined copy
would also be invisible to every mechanism the plugin has: `_collect_rom_items` migrates from the install records rather
than by walking the tree, so an unrecorded copy is left behind at the old home when the RetroDECK path changes, and
nothing would ever reclaim it. At ROM sizes that is silent, unbounded consumption of the storage the user has least of.

**One case is out of reach by design.** A genuinely different dump under a different name, of different size, is not
found — and adopting it would be wrong, because it is not the content the row would claim.

**Adoption supersedes the sibling group's other install, for the same reason downloading does.** One downloaded version
per shortcut binding is a stated rule, and an adopted row is a row like any other — leaving adoption out would make it
the single route that breaks the rule, with the group self-healing only by accident on the next download. The dialog's
promise not to delete does not reach the superseded sibling: that sibling is content **the plugin itself downloaded**,
re-fetchable from RomM, sitting at a different path — the same class the register's cleanup carve-out already covers.
What the dialog protects is the file the user placed, at this ROM's own location, which is precisely what the server
cannot hand back. The ordering is the part that had to be got right: every reason the adoption could be refused is
settled before the supersede runs, because a supersede followed by a refusal would destroy a working version and leave
nothing bound, and a supersede after the row write would leave two installed versions if the removal failed.

**Adoption becomes a sixth recorded-state writer site for `applied_launch_options`.** One class of install row means one
class of Steam shortcut behind it: an adopted ROM's shortcut is written the moment the dialog closes, and the value the
frontend wrote is recorded exactly as the download path records its own. Leaving it `NULL` would have been precisely the
difference this decision rules out — the same install, distinguishable afterwards by how it was produced, and the next
sync re-touching one shortcut but not the other. It adds no new writer: it is
`RomInstallRecorder.do_record_applied_launch_options`, the download path's own method, reached from a second flow at the
same point — after the install commits, before the frontend's write lands. The register in `CLAUDE.md` counts the flows,
which is why it reads six.

## Alternatives considered

**Record provenance and refuse to delete what the plugin did not download.** Rejected. It reads as the cautious choice
and produces the opposite: installs the user cannot remove through the UI, and an Uninstall button that sometimes does
not uninstall. It also moves the safety check from one place to every consumer.

**Adopt on filename alone**, as #260 originally proposed. Rejected once adoption was settled as carrying deletion
authority. A name proves nothing about content, and the row it writes is what a later removal acts on.

**Gate the dialog on a size match.** Rejected: it reintroduces the defect. A size mismatch means the file in the way is
_not_ ours, which is the strongest reason to ask, not a reason to overwrite silently.

**Verify automatically before offering a candidate.** Rejected: on a multi-disc set that is tens of seconds between the
click and any dialog. The obvious repair — hash automatically below some size — is a threshold nobody can justify.
Deciding on cheap evidence and putting verification on a button needs no threshold and never blocks.

**Quarantine the replaced file** into a `.romm-backup`-style store, as save-sync does. Rejected. Saves are kilobytes
with a ten-copy retention; ROMs are gigabytes with no retention that makes sense, the copy is invisible to migration and
cleanup alike, and setting it aside overrides a decision the user was just shown enough information to make.

**Leave multi-file out**, as #260 scoped it, on the grounds that completeness cannot be inferred from the filesystem.
Rejected because the premise no longer holds: RomM's per-file manifest states exactly which files must be present and
what each contains, which makes a directory _more_ verifiable than a lone file, not less. Leaving it out would also have
left the silent merge — and the uninstall that deletes the user's unrelated files with it — in place.

## See also

- [ADR-0007](0007-rom-retention-identity-anchor.md) — why an uninstall drops only files and the install record
- [ADR-0008](0008-rom-install-launch-file-and-rom-dir.md) — `file_path` / `rom_dir`, and why single-vs-multi is read
  from `rom_dir` presence
- [ADR-0027](0027-claim-discipline-follows-the-recovery-bundle.md) — the claim discipline a removal applies to the files
  an adopted row points at
- [CONTEXT.md](../../CONTEXT.md) — **Adopt**, **Adopted install**, **Adoption candidate**
