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
three digests per member **non-optionally**, so the server knows the content inside its own archives. Per-member hashes
have been stored since RomM 4.9.0, but the column is filled by a **scan**, not by the upgrade: a live 5.1.0 instance
sends `archive_members: null` for every archived ROM in a library it has not rescanned since, which is why the
file-level digest and not the member list is what the plugin depends on. Hashes are computed by default and opt out
through `filesystem.skip_hash_calculation`.

Cost was measured rather than assumed, and the shape of the answer is what matters here. Hashing is I/O-bound at the
storage's read rate and identical for CRC32, MD5 and SHA-1, so a disc-sized image costs tens of seconds while a
cartridge-sized ROM is imperceptible — a spread wide enough that a verification cannot be a wait imposed before the
dialog opens. A ZIP's central directory yields each member's uncompressed size and CRC32 without decompressing anything,
and a library keeps enough of its ROMs archived for that path to be the common one rather than the exception. Platform
directories hold a handful of subdirectories, while a single multi-file install can hold tens of thousands of files —
which is why any directory scan must stay on the top level.

## Decision

**An adopted install is a `rom_installs` row like any other, deletion authority included.** Uninstall and removed-game
cleanup delete an adopted ROM's files exactly as they delete a downloaded ROM's. There is no provenance column and no
downstream exemption.

**The protection therefore sits at the moment of adoption**, in a dialog the user cannot skip. It opens whenever
something is in the way:

| Trigger                                                                               | Answer                           |
| ------------------------------------------------------------------------------------- | -------------------------------- |
| A target path is occupied (multi-file has two: the extract dir and its collapse name) | Ask                              |
| An entry elsewhere in the platform directory carries this game's name, tags stripped  | Ask, and offer it as a candidate |
| Neither                                                                               | Download                         |

**The candidate search is keyed on the name, not on the size.** A size is what _ranks_ a candidate, not what finds one:
keying the search on it would miss the whole common case — a different rip of the same game, which is exactly what a
differently-tagged filename usually denotes — while matching any unrelated file that happens to be the same length. The
name is also the only key a **directory** has, because the search may not descend to total one (a single multi-file
install can hold tens of thousands of files) and a directory therefore has no size to compare.

**What "the same name" means is a normalization, and stripping the version tags is the point of it.** Bracketed groups
go with their contents, everything non-alphanumeric collapses to one space, and the result is lowercased:
`Example Quest - Second Journey (Rev 1) (USA).zip` and `Example Quest - Second Journey (U).zip` both reduce to
`example quest second journey`. We are looking for the _game_; whether it is the same dump is what the digest answers,
on the button. Normalized-name collisions concentrate _within_ a sibling group — the region, language and revision
variants of one game — and the search runs over the **platform folder**, not the library, so zero or one candidate is
the expected result; several open a short list ranked by evidence, each row stating what it rests on, and a capped list
says so — a silent truncation reads as "that is all there is".

**The search stays on the platform folder's top level, and admits an entry by a positive test.** Not descending is the
same tens-of-thousands-of-files constraint that keeps `describe_path` off the search's path, and a user's own subfolders
are their filing rather than ours. What survives is what ES-DE's live per-system `<extension>` list accepts, so
`systeminfo.txt`, `.directory` and every other frontend's bookkeeping fall out without a blacklist anyone has to
maintain. An accept-list that could not answer skips the test rather than turning the feature off — the name match plus
the user's confirmation is what the offer rests on, and this is the same default-safe reading every other consumer of
that list applies. A directory is never extension-tested (it usually carries none) and the candidate's shape must match
what the server serves, so a shape the dialog would refuse as unusable is never offered in the first place.

**Adopting a candidate renames it to the canonical name and carries its saves and savestates with it.** This is a
lifecycle argument, not tidiness. `compute_local_save_target` derives a save's filename from the **local** ROM's
basename — deliberately, because that is the string RetroArch uses to look up SRAM — so a game adopted under the user's
own name saves under that name. Leaving it there does not avoid a problem, it defers one: uninstall drops the ROM and
its row but never the saves (ADR-0007), so adopt `(U).zip` → play → save `(U).srm` → uninstall → later download
`(USA).zip` leaves RetroArch looking for `(USA).srm`, at a moment where nothing explains why. With the rename, an
adopted install is what this ADR says it is — indistinguishable from a downloaded one.

**The savefile and savestate directories are read independently.** RetroArch sorts them with separate keys, and on a
stock RetroDECK install the two disagree: `sort_savefiles_by_content_enable=true` beside
`sort_savestates_by_content_enable=false`. Deriving one from the other would send every savestate rename to a directory
nothing has ever written to. One rule covers both shapes, because the resolver already takes its root and both sort
flags as parameters: a single-file ROM keeps its save directory and changes its filenames, while a content-sorted
multi-file ROM keeps its filenames and changes its save _directory_ — the directory is named after the ROM folder being
renamed, and the launch file inside it never moves.

**Savefile sorting is read from the save sync's own answer, not from the live config.** A rename has to know where the
existing files _are_; the live `retroarch.cfg` says where RetroArch will write _next_. Those differ in exactly one
situation — a pending save-sort migration, where the files are still in the old layout and `RomInfoService` deliberately
keeps resolving against it (#238) — and that is the situation that matters: a rename reading the live config would move
the saves into the new layout, leaving the sync looking in the old one and the pending migration reaching for files that
are no longer where it left them. Nothing gates this out, because `@migration_blocked` covers the RetroDECK **home**
move only. So the sorting crosses a seam (`SaveSortingProvider` over `RomInfoService.current_save_sorting`) rather than
being read a second time: two implementations of "which layout is current" is precisely the drift that would reintroduce
it.

Two things stay on the live config, and the asymmetry is deliberate rather than an oversight. `savefiles_in_content_dir`
has no recorded counterpart — the change-detection markers are written only for the `InSaveDir` case, because
content-dir saves sit outside the tree the plugin syncs at all. The **savestate** layout has none either: those markers
track savefile sorting only, so a pending savefile migration says nothing about where savestates are.

**Download Instead removes what the user was shown, wherever it sat.** The second confirmation names a deletion, so
there is one rule for both subjects of that dialog: content at the target path and a candidate beside it under another
name are removed alike, under the same containment guard, with a failed removal aborting the download rather than
proceeding. The alternative — deleting only the target-path case — leaves a dialog that says "if it is your own dump,
patch or romhack, it is gone" and then downloads a second copy alongside the file it named.

A discarded candidate's **saves travel too**, to the canonical name, through the machinery the adopt exit uses. That is
not a second decision: saves follow the game, settled above, and the download exit strands them under the old basename
otherwise — the same orphaning the rename exists to prevent, arriving through the other door. A name already taken
raises the same collision question, answered before anything moves, because it is the same question. The carry runs
**before** the removal: a carry that fails aborts with nothing deleted, where removing first would leave the saves
orphaned under a name whose ROM is already gone.

One case carries nothing, and it is a limit rather than an oversight. A **multi-file** ROM's saves are named after the
launch file _inside_ its directory, and the launch file the download will produce is in an archive that has not been
fetched yet — so the name they would have to take is genuinely unknown at this point. Moving them to the candidate's own
launch name would strand them under a name nothing reads, which is worse than leaving them untouched and findable.

**A name already taken stops everything before anything moves.** Every source → target pair is computed and **all**
targets are checked, and only then does the first file move: renaming as you go and asking at the first collision leaves
half the set moved when the question appears. The dialog lists everything that collides and takes one decision for the
whole set — overwrite, keep (stating that the old-named files are now orphaned rather than implying the move was clean),
or cancel. The ROM's own target is not part of that question: something that arrived there is the occupied-target case
the first dialog owns, so it is refused outright rather than offered as something to skip or overwrite. An Overwrite
clears its targets **before** the move rather than replacing as each file lands, which keeps the one destructive phase
the user answered for apart from the phase that must not be destructive.

**A replaced save is backed up, not deleted — and this is the one place that argument goes the other way.** The
register's rule is backup-**or**-confirm, and this path does confirm by name, so a plain delete was inside the rule. But
every reason given above for _not_ quarantining a ROM is a reason **for** quarantining here: a ROM is gigabytes with no
retention that makes sense and is re-fetchable from RomM, while a save is kilobytes with a ten-copy retention already
built — and a **savestate is synced nowhere at all**, so a replaced `.state` exists in no other copy anywhere. So an
Overwrite routes through `MatrixExecutor.quarantine_local_file`, the same funnel the sync's own download-overwrite and
slot-switch paths use. Deliberately not a second implementation of it: a new way to destroy a save, beside the one that
documents itself as the single source of that discipline, is how the first one stops being the discipline (#965).

Every colliding target is a save or a savestate by construction — the ROM's own target is refused before the plan is
consulted, and the download exit drops the ROM pair — so the rename machinery needs no delete of its own, and has none.

This is the funnel's first caller outside the saves root. It takes whatever directory it is given and writes
`<dir>/.romm-backup/`, so a savestate's backup lands under the states root. Two limits of its naming follow the savefile
it was written for, and are stated rather than bent: the backup name is `<stem>_<ts><ext>` from a single `splitext`, so
`Game.state.auto` is preserved as `Game.state_<ts>.auto`; and the ten-copy retention counts per that stem, which for
savestates means per slot suffix rather than per game.

**A rename is atomic per file, so the failure is moved somewhere harmless rather than prevented.** Where the filesystem
allows it: `os.link` every pair first, removing nothing, so a failure while staging is undone by dropping the links and
the state is exactly as it started; only once every link exists are the originals unlinked, and a failure there leaves
two names for one inode — no data lost, and a re-run finishes it. Hardlinks need one filesystem and cannot name a
directory at all, so a directory ROM, or a device with saves on internal storage and ROMs on an SD card (`EXDEV`), falls
back to rename-with-rollback. That path can leave a genuinely partial state, and it is reported by name — which files
moved, which did not — never as success and never as a plain failure, the way the prune machinery reports a partial
removal.

**Cheap evidence decides what to say about a candidate; content verification is always a button, never a wait imposed
before the dialog.** Whether to ask at all is settled by the name (above) and by a `stat` of the target path. What the
dialog then _states_ about each candidate is only what a read already paid for: a `stat`'s size, and — where the content
is archived — the uncompressed size and CRC32 the ZIP's central directory hands over at no cost. A server that published
no hashes simply leaves the strongest row unavailable; it does not turn the search off, because the name match and the
user's confirmation are what the offer rests on either way. The directory case is verified against the server's full
manifest, matched against `is_top_level`, with extra files allowed — the plugin's own directories carry a generated
`.m3u` and a healed `PS3_DISC.SFB` that the server's manifest does not list.

**A content check compares content identity, never container identity.** RomM hashes what is _inside_ an archive: its
current scanner streams every member's decompressed bytes, in ASCII name order, into one accumulator, and the one before
4.9.0 took the archive's largest member. Either way the file-level digest describes the content while `file_size_bytes`
is the container's size on disk. The two describe different things, which is why the size agrees and the digest cannot:
a zipped ROM the plugin downloaded from RomM itself reports a mismatch against the very bytes RomM sent.

**The file-level digest is the carrier; `archive_members` is an optional extra.** Measured against a live RomM 5.1.0
instance, `archive_members` is null for every archived ROM: the column arrived in 4.9.0 and stays null until the library
is rescanned, while `files[0].crc` already equals the member's CRC32 in the ZIP's own central directory. So the
comparison is keyed on what is on disk — anything whose **name** says archive is opened, and RomM's `ARCHIVE_READERS`
extension set is what "says archive" means, because that set is exactly where its digest describes contents. Neither the
container's digest nor its size is compared: two archives of the same ROM differ in both whenever they were packed
differently.

**What can be claimed depends on what is inside.** One member: its digest _is_ the archive's content identity under
every rule RomM has used — the composite over one member and the largest of one member are the same bytes — so this is
decidable, and it is the common case for a zipped single-ROM library. Several members with `archive_members` stated:
each is held to its own entry, and members on disk the server did not list are allowed, because its scanner drops
excluded names and extensions. Several members **without** them: `unverifiable`. The exclusion set is reproducible — a
fixed pair of module constants, eight extensions and seven names, not the user-configurable list — but reproducing it
would not settle which rule produced the number, and the evidence says these rows come from the pre-4.9.0 rule, where
agreement covers the largest member and says nothing about the other thirty-nine in an arcade set. `match` states that
_these files_ match; a rule that cannot support that sentence must not be allowed to print it.

**Cheap evidence disqualifies; the digest RomM published is what qualifies.** A member's uncompressed size and CRC32
come from the central directory at no cost, and either disagreeing is proof enough to report without decompressing
anything. Agreement is not the converse: CRC32 is a 32-bit checksum, at a library's member counts an accidental
collision is credible, and this comparison authorises a row carrying deletion authority — so what earns `match` is the
MD5 RomM published beside it, over the member's decompressed bytes. That is the same preference the whole-file path
already applies, extended inside the container rather than weakened to fit it.

**What cannot be enumerated is reported as unconfirmed, never as a mismatch.** A container this plugin cannot open — a
`.7z`, a `.rar`, one damaged since it was listed — leaves the entry unchecked, which is `unverifiable`. The one
exception is a single-member archive whose member's uncompressed size the file on disk matches exactly: that is the user
who unpacked what the server keeps packed, and the loose bytes are that member's. Accusing content of differing on bytes
that were never read would be the strong claim, and it has not been earned. `unverifiable` therefore carries three
distinct messages — the server published no checksums, the plugin could not read what they speak for, or the number
covers a whole archive it cannot attribute — because a user told "your server publishes no checksums" about a server
that does will go looking for a problem that is not there.

**The dialog has three exits.** Adopt records the existing content as the install. Download replaces it, behind a second
confirmation naming the deletion. Cancel does nothing. For multi-file, Download removes the existing directory and
extracts fresh — the user chose replace, not merge.

> **Amendment (the search runs on the game page too, and the button keeps its own promise — #260 PR 2).** The decision
> above settled what happens once a candidate is offered. What follows settles when the offer is made, and what is said
> when it cannot be. Nothing above changes.
>
> **The game detail page runs one `stat` and, when that comes back free, one `readdir`** — both only when no install row
> exists, so the page can say a ROM is already on the device rather than offering an undifferentiated Download, whether
> it sits at the computed target path or beside it under another name. Neither read is a guarantee: an unreadable
> folder, an unresolvable ROMs root, a directory that is not an ES-DE system and an accept-list that could not answer
> all report **no** candidate, because a search that could not run must never make a game look uninstallable. The
> `readdir` is a bare directory read — name and directory-flag per entry, and no `stat` for the size and mtime, because
> a name match reads neither — plus string work; the archive-index read that _ranks_ candidates costs an order of
> magnitude more per entry and is the dialog's, not the page's. The whole payload is assembled on a worker thread, which
> is also where the Unit of Work's connection is meant to live (ADR-0004).
>
> **The two searches are not held to agreeing, and the promise does not rest on them agreeing.** They answer the same
> question from different knowledge on purpose: the page holds a `roms` row and must stay network-free and instant,
> while the click-time search holds the server payload and the path the download derived. Four times that difference
> produced a real divergence — the shape RomM serves, the platform directory, the name matched, and the directory
> listing itself. Each was closed as it was found; what does not follow is that the next one has been.
>
> So the guarantee is stated differently. It is not "the two agree"; it is **pressing the button always ends in an
> answer**. That is enforced in one place — the last check in the chain below — and every other refusal exists to
> replace a generic answer with a specific one, never to prop up an invariant over two searches that cannot be made to
> know the same things. The label may still overpromise (**Use Existing Files** for something that turns out not to be
> usable); what it actually promises is that pressing opens a dialog rather than starting a transfer, and that holds.
>
> **The chain, most specific answer first.** The search runs again at click time — the folder can change while the page
> is open — and returns the first of these that applies:
>
> 1. `adoption_candidates` — entries of the served shape that an install row may point at. The dialog offers them.
> 2. `unusable_namesake` — this game's name on something that cannot become the install: the other shape, or a symlink.
>    The user is told what is there and chooses between downloading a second copy beside it and stopping.
> 3. `candidate_vanished` — the backstop. The page reported a copy and neither of the above applies, so the download
>    stops and says so instead of starting silently. It claims no cause, because none is known; it also covers the
>    ordinary race where the file was deleted between opening the page and pressing.
>
> **The directory has to be a system.** Before searching, the plugin asks `es_systems.xml` — the source the accept-list
> already comes from — whether the directory it is about to read is a system at all. An unmapped RomM slug is otherwise
> taken verbatim as a directory name, and a namesake inside such a directory is content no emulator will ever look at.
> The same check closes a second hole at no extra cost: an empty accept-list means "could not tell" and skips the
> extension test, which is right for a real system and removes the filter entirely for a directory that is not one. A
> source that could not answer is not a denial, and the search proceeds.
>
> **Storing the served shape on the `roms` row was considered and rejected.** It looks like the fix that removes the
> shape disagreement at the source, and it does not work: the ROM **list** endpoint the sync pages through carries an
> empty `files` array, so a shape computed at sync time would fall through to `has_multiple_files` — a flag that is
> wrong by construction for the case its own docstring names, one file at the ROM root plus files in subfolders (a
> Switch title with `update/`, a PS3 title with updates), which RomM zips while the flag says single. That would give
> the page a second, disagreeing source of truth for a question `domain/rom_files.py` exists to answer once, and would
> leave exactly that class still lying. Refusing the disagreement at click time covers the nested-single case too, which
> no stored shape could.

What none of that settles is what counts as an entry in the first place — the question every answer above assumes has
already been decided.

> **Amendment (an entry is judged by what it is — #260 PR 2).** The search had acquired one special case per review
> round. Most of them were answering a problem created by admitting entries that should never have been visible, so the
> admission rule replaces them and they are deleted rather than repaired.
>
> **Judged by its own type, without following it.** An entry is a **file**, a **directory** or a **symlink**, and
> anything else — a FIFO, a socket, a device node — gets no kind at all. The directory read already carries the type, so
> this costs nothing; resolving the entry instead would cost a syscall per link and, worse, re-admit a link as ordinary
> content. "File or directory" has no truthful answer for a named pipe, and inventing one is what let a pipe carrying a
> game's name be offered as that game, with a size of zero and the evidence line "Matched on name only".
>
> **One function answers it, and every read of the filesystem asks that function.** This was first written as a rule and
> re-derived per caller, so the door the rule was written for was closed and the one beside it stayed open: a pipe at a
> ROM's own target path was still described as an ordinary file and still offered. A rule with two implementations is a
> rule with one exception nobody has found yet, which is why the answers are shared rather than agreed.
>
> **A kindless entry is dropped from a listing and reported by a description.** That is not two rules but two questions:
> a listing answers "what is here", where something with no name for it is nothing to offer, and a description answers
> "what occupies this exact path", where the only wrong answer is "nothing" — that is the answer a write then acts on.
>
> **A symlink is mentioned and never adopted.** Every uninstall goes through `claim_source`, which refuses a symlink
> outright, so an install row pointing at one could never be removed through the UI — the outcome the decision above
> rejects by name. But it is content the user has, and a download lands beside it and leaves them two, so it is named in
> the same dialog the wrong shape uses: one refusal for "this cannot become the install", whatever the reason.
>
> **This holds at the ROM's own target path too**, where the occupied-target dialog reaches the same content without the
> search. Existence is answered without following, so a link there is reported as occupying its path and marked
> unadoptable, and the dialog names it for what it is: the right noun, no size verdict computed from a number that is
> the length of a stored path rather than a byte count, and a replace warning that says only the shortcut goes. That
> also ends a silent destruction: a link whose target did not resolve was described as "nothing is here", and the
> finalize `os.replace` then overwrote it without a word.
>
> **The check runs again immediately before the adoption, not only before the dialog.** The entry offered as a regular
> file can be a link by the time the user confirms, and the offering sites cannot cover that. So the acting site asks
> the same question — kind and shape in one predicate — rather than trusting a disabled button.
>
> **The deletion offer is gone, and the "cannot be read" outcome with it.** Deletion was offered for a link whose target
> did not resolve, on the grounds that it holds no data. The proof does not exist — "the target is not there" and "the
> target is on a drive that is not plugged in" are the same answer from the operating system — and the act bought
> nothing, because the entry is beside the target path and the download lands on the canonical name either way. The
> outcome that carried it went too: under the admission rule the bucket is empty on a static filesystem, and what
> remains — an entry deleted between the directory read and the description — is a search that came up empty, which the
> backstop already answers.

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

**One case is out of reach by design.** A copy whose name differs by more than its version tags is not found —
`Example Quest` is not offered for `Example Quest - Second Journey` — and it should not be, because it is a different
game. Closing that gap means fuzzy matching, and a fuzzy match that is wrong writes a row carrying deletion authority
over content the server cannot hand back.

**The plugin renames files the user placed, which is a second thing it does to content it did not create.** It is the
direct consequence of one class of install row: a row whose filename disagrees with the server's is an install that
behaves differently from every other one, and the difference surfaces later, as an orphaned save, at a moment nothing
explains. The rename is stated before it happens, in the same dialog that carries the comparison, and every collision it
would cause is a second question rather than a silent overwrite.

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

**Adopt a candidate where it lies and record the user's filename.** Rejected — it is the option that looks like it
respects the user's filing and actually defers the cost. The row would be correct on the day it is written and wrong the
first time the game is uninstalled and re-downloaded, with the saves under a name nothing looks for and no dialog
anywhere near the moment it goes wrong.

**Fuzzy-match beyond the tag normalization** — edit distance, token subset scoring. Rejected: the row an adoption writes
carries deletion authority, and the search's own output is what the user is asked to confirm. A near-match presented as
a candidate is a suggestion the plugin cannot support, and the failure it enables is destroying a file the server cannot
replace. If the normalized names differ, it is not a candidate.

**Ask per collision instead of once for the set.** Rejected on ordering rather than on taste: the first question can
only be asked after the first rename, so a user who then cancels is left with a half-moved set and no dialog describing
it. Computing the whole plan first costs one extra `stat` per pair and makes cancel mean nothing happened.

**Copy-then-delete instead of link-then-unlink.** Rejected for a ROM-sized file: it needs the space twice over on the
storage a handheld has least of, and it is slower by the whole size of the game. The hardlink stages the same "both
names exist" safety at no cost, and the case where it genuinely cannot — a directory, or two filesystems — is the one
case that falls back to a rename, where the rollback is cheap for the same reason.

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
