# BIOS and Emulator Core Management

Some emulated systems require BIOS files to run games. Without the correct BIOS files, games for those systems will fail
to launch. The plugin can download BIOS files directly from your RomM server.

Which BIOS files a system needs depends on the **emulator core** in use — some cores need BIOS, some don't. Because the
two concerns are related but independent, the plugin presents the **active core** and its **BIOS state** together in one
place: the **Platforms** tab of the **Library** page, where picking a platform on the left shows everything about it on
the right. Core selection and BIOS file management can each be used on their own — they share a pane only because the
active core determines which BIOS files matter.

## What Are BIOS Files?

BIOS (Basic Input/Output System) files are firmware dumps from original hardware. Emulators need them to accurately
simulate the console's boot process. Common examples:

- **PlayStation** — `scph5501.bin` (and other regional variants)
- **Dreamcast** — `dc_boot.bin`, `dc_flash.bin`
- **Saturn** — `sega_101.bin`, `mpr-17933.bin`

Not all systems need BIOS files. Cartridge-based systems like Game Boy, SNES, and Genesis typically work without them.

## BIOS Status on the Game Detail Page

When you open a game whose platform has BIOS files — on your RomM server, or asked for by the emulator that will run it
— the game detail panel's **BIOS** tab shows the readiness line. Its dot color reflects the same
unknown/ok/partial/missing verdict used everywhere in the plugin:

- **Green** — nothing required is missing: "All required ready (2/2)", or "Nothing required (3/5 files held)" when the
  core you launch with requires none of the system's files
- **Orange** — some required files present: "1/2 required files ready"
- **Red** — no required files present yet
- **Grey** — no readiness claim, in one of two wordings: "BIOS requirement unknown", where the plugin could not work the
  requirement out at all (see [When the requirement is unknown](#when-the-requirement-is-unknown)), or "BIOS readiness
  unknown", where it knows the requirement and could not settle whether you have it (see
  [When readiness cannot be stated](#when-readiness-cannot-be-stated))

The sentence says what the dot says, and both are about the **required** files. Where the system has none, the dot is
green because nothing required is missing — so the line leads with "Nothing required" and the ratio beside it is
inventory, counting the files your RomM library holds and how many of them you have. It is not a readiness score, and
those files are not "optional" either: a core you are not launching with may well require one.

Both numbers on the line count the same thing. Where the system has required files the ratio is of those; where it has
none, the ratio is of the files your RomM library holds — never one of each. Files your emulator asks for that are not
in your library are listed, but they are not part of that ratio: it tracks what you can still download, and those you
cannot.

The readiness line is computed against the **active core** for that game — so switching to a core that needs no BIOS (or
that treats a file as optional) clears the warning, while switching to a core that requires a missing file surfaces it.

Beside the Play button there is also a short **BIOS** badge, which is a shortcut into this tab. It appears on one
condition and no other: a file the active core **requires** is shown to be absent from your BIOS folder. If that core
requires nothing, or requires only files you already have, there is no badge — however many optional files are missing,
and whether or not the requirement could be worked out at all. A required **folder** counts here like any other
requirement: once the plugin has established that it holds no BIOS image, the badge appears, because the game will not
launch. What raises no badge is a requirement nothing could settle — a folder the plugin could not read, say — since it
has not shown anything to be absent. Those cases are worth reading, but not worth a warning next to Play, so they live
in the tab.

The badge is always **red**. It is a warning, not a status: the four-colour dot above belongs to the tab's readiness
line, and every state that raises the badge is one the game will not launch in. Having one of three required files is
not a milder version of the problem, so it does not get a milder colour.

A BIOS warning only ever disappears on an **answer**. When a check cannot be run at all — most often right after a BIOS
download or delete, before the state has been read again — the plugin keeps showing the last status it knew rather than
reporting "no BIOS needed". So a check that could not be completed never quietly clears a missing-BIOS warning and lets
the game launch without its files; it leaves the warning where it was.

"The requirement is unknown" is itself an answer, and a different one. A check that ran and could not establish what the
system needs says so on the page — grey dot, "BIOS requirement unknown" — instead of hiding the BIOS tab. Hiding it
would say the system needs nothing, which is exactly the claim that could not be made. Every PS3 game is in that
position, since RPCS3 is not a RetroArch core and cannot be asked.

It does not leave it there indefinitely, though. Whenever the game detail page is shown a BIOS state it could not work
out — when the page opens, after switching the emulator core, after switching to another version of the game — it asks
again in the background and fills the answer in a moment later. So a game opened while its requirement is still unread
gets its BIOS tab a beat after the rest of the page, and a core or version switch settles on the new core's or new
version's readiness rather than the one before it.

An unreachable RomM server usually does **not** put the plugin in that position. Everything the readiness line needs is
local: which emulator is active comes from your RetroDECK configuration, what that emulator needs comes from the
emulator itself, and what you already have comes from your BIOS folder. Your server contributes the **download** — so
while it is unreachable you still see what is needed and what is missing, you just cannot fetch anything, and files that
exist only in your RomM library are not listed.

Below the readiness line the tab lists the individual files and which ones are present or missing. Each file lists the
cores that use it (e.g. _Beetle PSX HW (required)_, _SwanStation (optional)_); the **active core**'s line is highlighted
in amber so you can spot at a glance which core's requirements the file applies to.

Files no installed emulator asks for are not listed one by one — they are summarised on a single line below the list ("3
files on server no installed emulator asks for"), as are any files the plugin could not work out an answer for.

A file your emulator needs that is **not in your RomM library** is listed too, marked _not in your RomM library_. The
plugin cannot fetch it — nothing in it can — so it is shown for what it is rather than left out. Adding the file to RomM
makes it downloadable like any other.

Not holding a file is a different question from not having it, and the row says both. If it is also absent from your
BIOS folder it reads _missing, not in your RomM library_; if it is already sitting there it reads only _not in your RomM
library_, with the green dot every present file gets.

A red row has a second cause worth knowing about. Where the plugin could not look at the place a file goes at all —
broken permissions, or storage going bad — the row says _its location could not be read_ rather than calling the file
missing, so you know to check the folder instead of hunting for a download. It still counts as not ready, because an
emulator will not get in there either.

Some rows say something better than that. Where the file sitting at the destination is byte-for-byte the copy your
emulator distribution ships, the row names the distribution instead — _provided by RetroDECK_ — because it is the
distribution's file, and if it ever went missing the repair is a RetroDECK component reset rather than a download.
`dolphin-emu/Sys/codehandler.bin` is the usual example, and that one no library holds; others are perfectly ordinary
files you may well have in RomM too, and the row still names the distribution, because whose copy is at the destination
is the more useful fact. The name is printed exactly as the plugin's emulator-knowledge library writes it.

A row can also be a **folder** rather than a file — LRPS2 asks for `pcsx2/bios`, which is where your PS2 BIOS files go.
A folder is satisfied by what is **inside** it, never by the folder being there, so the plugin opens the files in it and
reads them the way the core does:

- A folder holding a PS2 BIOS image is green, and the row lists what it found underneath itself, one image per line and
  in the emulator's own words — _USA v02.00(14/06/2004) Console 20040614-100909_ and so on for each. The core needs
  exactly one of them, so none is marked required, and which one your emulator loads is a core setting the plugin does
  not read.
- A folder holding no image is red, exactly like a missing file: _holds no BIOS image_. That is the honest answer for a
  PS2 system that will not boot, and it is what the red **BIOS** badge beside Play appears for. The plugin does not
  always have to read the files to say it — a folder holding nothing even the right size for a BIOS is answered by their
  sizes alone — and the row reads the same either way.
- Where the read could not finish — a file whose bytes would not come back, a folder that could not be listed in full,
  or an image the plugin's identity table and the emulator's own check disagree about — the row says so and gets an
  amber dot, and the system's readiness line declines (see
  [When readiness cannot be stated](#when-readiness-cannot-be-stated) below). Nothing is being claimed either way.

A folder that is **absent** is red like any other requirement that is not there, with no note beside it: there is
nothing to have found, so there is nothing to say. It is never offered as a download either — what the emulator opens
there is a folder, so there is no file to fetch into it. On a stock RetroDECK that will not happen for `pcsx2/bios`,
which RetroDECK links onto the BIOS folder itself. (The Library page's platform detail words that row _Missing_, because
it spells absence out where this tab leaves it to the dot.)

<!-- Screenshot: Game detail page showing orange BIOS status with "2/5 required files ready" -->

![BIOS file list overlay showing individual required files with checkmarks and "Missing" labels](../assets/screenshot-bios.jpg)

## Library › Platforms

The **Platforms** tab of the **Library** page holds everything about one system: its sync toggle, the **active emulator
core**, the BIOS files that core needs, and the two ways to take the platform back out of Steam. The list on the left
holds every platform your RomM server reports with at least one ROM, in two groups — **Synced** above **Available** —
and the row you focus is the one the right-hand pane describes.

1. From the main QAM page, tap **Library**, then move to the **Platforms** tab with L1/R1
2. Each row is a coloured dot, the platform's name and the sync toggle. The dot is the BIOS state at a glance — green
   ready, amber partly there, red missing, grey where there is nothing to say — and the numbers behind it are on the
   right-hand pane, which also states them in full. With a mouse, hovering the row says the same thing in words
3. Move down the list to pick a platform; the pane on the right changes with the focus
4. The pane's first line names the platform, how many ROMs it has on RomM, how many are in Steam, and the core it
   launches with
5. **Change core** opens a menu of the platform's emulators. It appears once the platform has games in Steam — with
   none, a sentence asks you to sync it first
6. **BIOS files** states how many required files are ready (e.g. "1 / 2 required") when the system needs any, and
   otherwise reads "Nothing required" with the inventory of your library's files beside it (e.g. "3 / 5 files held"). A
   system with a required row the plugin could not judge — a declared folder it could not read, say — reads "BIOS
   readiness unknown" instead — see [When readiness cannot be stated](#when-readiness-cannot-be-stated)
7. Below it, a table lists the files themselves: the **file**, whether it is **on disk**, and its **contents**. On disk
   holds marks and no words. The first mark carries two things — a green ✓ for a file that is there and required, a red
   ✗ for one that is required and is not, and the paler green ✓ / grey ✗ for a file the core you launch with does not
   need either way. Amber means nothing could be established: a ✓ or ✗ in amber is a file whose presence is known but
   which no installed emulator could be asked about, and a `?` is a row that could not be checked at all. A violet ⊘
   appears **beside** that mark — never in place of it — when your RomM library does not hold the file: a file you
   already have keeps its green ✓, one you still need keeps its red ✗, and the ⊘ adds that the plugin cannot fetch it
   for you. A legend under the table, one line per mark, names the marks that are actually on it. Anything else a row
   has to say is printed **under** the row rather than in the column — that a file was provided by RetroDECK, that a
   folder holds no image, that a location could not be read
8. **Contents** answers for a required **folder**: how many BIOS images it holds — and the images themselves are listed
   under the row, in the emulator's own words so you can match one against its picker — or that it holds none, or that
   its contents could not be established. A plain file reads an em dash, which means the question was never asked:
   checking a file's contents is still to come, and until it lands the em dash must not be read as "checked, and nothing
   there"
9. A **Download** button sits on every row that is missing and in your RomM library. **Download required** and
   **Download all** fetch several at once. A system nothing installed could answer for has no download buttons at all —
   see [When the requirement is unknown](#when-the-requirement-is-unknown)
10. **Delete BIOS** removes that platform's downloaded BIOS files (see below)

<!-- Screenshot: Library › Platforms with a platform selected, its core button above the BIOS table -->

BIOS files are downloaded to your RetroDECK bios directory (e.g. `~/retrodeck/bios/`). Some platforms use
subdirectories: Dreamcast BIOS goes into `bios/dc/`, because that is the location the Dreamcast core declares. The
plugin handles the correct placement automatically — the location is the one your emulator itself declares, so a file
for a subdirectory lands in that subdirectory rather than loose in the BIOS root.

PS2 looks like a subdirectory and is not one. Nothing declares a location for an individual PS2 BIOS dump, so the plugin
puts it in the BIOS root — and RetroDECK links `bios/pcsx2/bios` back to that same folder, which is why the file appears
in both places at once. There is one copy, not two.

### Deleting BIOS Files

You can remove a platform's downloaded BIOS files from its pane in **Library › Platforms**. The **Delete BIOS** button
appears only when there is at least one file it would actually remove — a file the plugin downloaded that is still on
disk — and its label shows how many (e.g. "Delete BIOS (3)"). Because deletion is local, the button works even when your
RomM server is offline.

1. In **Library › Platforms**, pick the platform whose BIOS files you want to remove
2. Tap **Delete BIOS**
3. Confirm the action in the dialog that appears

This is a destructive action, so a confirmation dialog asks you to confirm before anything is deleted. Once confirmed,
the plugin removes the BIOS files **it downloaded** for that system from your RetroDECK bios directory and reports the
result. Games that need those files won't launch until you download them again with **Download All** or **Download
Required**.

**It only ever deletes its own downloads.** A file is removed when the plugin has a record of downloading it, and for no
other reason. Everything else in the BIOS folder is left alone, including:

- **Firmware RetroDECK ships itself.** RetroDECK installs some files into the BIOS folder with its own components —
  `bios/dolphin-emu/Sys/codehandler.bin` is one. Your emulator asks for it, so it is listed on the page, marked
  _provided by RetroDECK_, but the plugin did not put it there. That one in particular is in no RomM library either, so
  nothing here could fetch it back.
- **Files you placed there by hand**, even where the name matches one your RomM library holds. Without a download record
  the plugin has no claim on it.

Being in your RomM library is neither necessary nor sufficient. A file you have since removed from RomM is still deleted
if the plugin downloaded it — otherwise its own downloads would be stranded on disk with no way to clean them up — and a
file that is in your library but arrived some other way is left where it is. If you want one of those gone, delete it in
the file manager.

The count on the button is the same set: it counts the plugin's own downloads that are still on disk, so it matches what
the delete reports. A file it downloaded that you have since removed by hand does not appear in it, and the leftover
bookkeeping entry is cleared the next time you run the delete.

This is the only place a platform's BIOS files are deleted. The Data Management page used to offer the same action
without a confirmation; that copy is gone.

## Which Systems Need BIOS?

This depends on your emulators, not on your library. Common systems that require BIOS files include PlayStation, PS2,
Saturn, Dreamcast, and some arcade systems. A platform appears with a BIOS status when your RomM library holds firmware
for it **or** one of its emulators asks for a file — so a missing BIOS you have never uploaded is visible rather than
silent.

### Where the plugin gets its answers

The plugin asks your **installed RetroArch cores** what they want. Every core ships a small description file next to it
declaring the firmware it needs and where each file goes, and the plugin reads those live — so the answers follow your
RetroDECK install, including cores that were added after the plugin was released.

That same reading also answers whether a **declared** file is already sitting where the emulator will look for it, and
for those the plugin takes its answer rather than checking the path itself. The difference matters on a stock RetroDECK:
`bios/pcsx2/bios` is a link pointing back at the BIOS folder, so working the location out from where a link ends up
loses the folder the emulator actually opens. Following the emulator's own spelling gets the check right. The rest the
plugin looks up itself, because there is no reading to take: files in your library that nothing asks for, and files an
emulator keeps somewhere outside your BIOS folder entirely.

Where a core asks for a **folder** rather than a file, that reading is not enough — a folder is satisfied by what is in
it — so the plugin asks a second, narrower question for just that core, and it opens the candidate files and reads them
the way the core does. It only asks it where there is something left to settle — a folder that is not there, one with a
file sitting where it belongs, and one holding nothing even the right size are all answered without opening anything.
Asking it for your whole BIOS folder every time a game page opened would mean reading every file in it, which is why it
is scoped this tightly.

**Standalone emulators are not asked.** RetroDECK also offers emulators that are not RetroArch cores — RPCS3, Vita3K,
Cemu, xemu and others — and they state their firmware in their own formats rather than in that one description file.
Reading them accurately is a separate piece of work, so rather than guess, the plugin says it does not know: a system
whose only emulators are standalone reads **unknown** (below), never "not needed".

Each file on your server therefore gets one of four answers:

- **Needed** — an installed core will not run without it
- **Optional** — an installed core can use it but does not require it
- **Not needed** — every RetroArch core this system offers was asked, and none of them wants this file
- **Unknown** — the plugin could not work out an answer (see below)

### When the requirement is unknown

"Not needed" and "unknown" are deliberately kept apart. The first is an answer; the second is the absence of one, and
the plugin will not present it as an all-clear.

A **file** reads unknown when the plugin could not ask every core the system offers — one of them ships without its
description file, or RetroDECK's configuration could not be read at all.

A whole **system** reads unknown, showing a neutral grey status and the text **"BIOS requirement unknown"** instead of a
green all-clear, in either of two situations.

The first is a system with files on the page, not one of which could be answered for. Every row on the platform is
unknown, which is what leaves nothing to base a readiness claim on. A single answered row is enough to keep the normal
green/amber/red status: if the system offers two cores and only one is unreadable, the other core's answers still stand
and only the unanswered rows read unknown. A system whose every file was answered with _not needed_ is not this case at
all — that is a finished answer, and it reads green.

The second is a system with **no files on the page at all**, where the plugin also could not ask every core the system
offers. An empty list means "nothing here wants anything" only when every core was asked; with even one of them unread
it means nothing, and reporting it as ready would be an all-clear over firmware nobody checked. This is the shape a PS3
page has when your RomM library holds no PS3 firmware: no rows, no cores to ask, and a grey "BIOS requirement unknown".
The system keeps its place in the platform list for the same reason — dropping it would say there is nothing to manage.

Note that the two shapes weigh an unread core differently, and deliberately: with files on the page one unreadable core
costs only its own rows, while with no files at all there is nothing left for the readable cores to have answered, so
the same gap takes the whole system to unknown.

Three causes reach these shapes. The common one is a system whose emulators are all standalone — PS3 through RPCS3, for
instance — where there is no core to ask in the first place. The second is a system all of whose cores are unreadable;
on a stock RetroDECK that is rare, since only a handful of bundled cores ship without a description file and just one of
them is offered for any system. The third only reaches the empty-list shape: a system with several cores, one of them
unreadable, whose page happens to have no rows.

This is informational, not an error: your files may be perfectly fine, the plugin simply can't confirm what is needed.
Genuinely BIOS-free systems (such as the NES) are unaffected — every core answers, none of them wants anything, and the
system reads as ready.

#### Such a system offers no downloads

A system in that state has no **Download All** or **Download Required** button, and says so in words: _BIOS management
is not supported for this system yet, so there is nothing to download here. You can still put BIOS files in your BIOS
folder by hand._ Fetching files the plugin cannot reason about — beside a line admitting it cannot — would be offering
to act on an answer it does not have. Downloading them from RomM's own web interface and dropping them in your BIOS
folder works exactly as it always did; nothing about the files changes, only what this page will claim about them.

This is scoped to the **whole system**, never to single files. A system whose reading finished may well hold files no
installed emulator asks for — a PlayStation page typically lists a good number of regional BIOS dumps that nothing wants
— and every one of those stays downloadable, because "nothing wants this" is a finished answer. The buttons go only
where there was no answer at all, and they come back the moment anything installed can speak for the system.

!!! note "Systems that used to read \"Not managed by the plugin\" will have moved"

    That state meant only \"no entry in the built-in table\", and the table is gone. Those systems now get whatever the
    installed cores say, which can go three ways: **green**, because no installed core wants any of those files, **red
    or amber**, because one does and the table simply never knew, or **grey**, because the system offers no RetroArch
    core to ask. All three are new, and all three are honest.

### When readiness cannot be stated

There is a second grey state, and it is a different sentence: **"BIOS readiness unknown"**. Here the plugin knows
perfectly well what the system needs — it is whether you **have** it that could not be settled for one of the required
things.

The usual cause is a required **folder** the plugin could not read all the way: a file inside it whose bytes would not
come back, a folder it could not list in full, or an image its identity table and the emulator's own check disagree
about. It is not the ordinary state of a PS2 system — a folder that reads cleanly is answered green or red like any
other requirement.

What the plugin will not do is guess at the part it could not reach. A folder whose listing broke off part-way might
hold a BIOS image in the part that was never read, or might not; calling it ready and calling it empty are both claims
about files nobody looked at. It declines instead, and says so.

What that state does **not** do is flatten the rest of the page:

- The **file rows keep their own answers.** A file that is present is still green, one that is missing is still red, and
  only the row nothing could be established for reads amber, with the reason beside it.
- **Downloads stay.** Every file your library holds is still fetchable, and fetching them is the thing that actually
  gets a PS2 system running. This is the opposite of the state above, where nothing could be answered at all and there
  was nothing to download against.
- The system is **not** flagged "BIOS needed", because that would be a claim too.
- The red **BIOS** badge beside Play still appears for a file that is genuinely missing — the unjudgeable row is simply
  not one of them.

## Which Files a Platform Lists

A platform's list has two halves. The first is what your **server** files under that platform — a GBA page lists the
firmware your server holds under GBA, not what it holds under Game Boy. The second is every file an **emulator RetroDECK
offers for that platform** asks for, wherever your server keeps that file and even when your server does not hold it at
all; rows in that half are marked as not being in your RomM library.

The second half follows what you can **launch** the platform with, not how a file is usually catalogued, and the two do
not always line up. RetroDECK offers NooDS under Game Boy Advance, and NooDS does run GBA games. Its description file
names five firmware files — `bios7.bin`, `bios9.bin`, `firmware.bin`, `nds_sd_card.bin` and `gba_bios.bin` — and a core
states its firmware once, for the whole core, with no way to say which of those a GBA game in particular needs. So a GBA
page lists all five: they are exactly the files an emulator you can start a GBA game with declares. Filtering rows out
by the system a file is usually filed under would, wherever such a file is genuinely required, leave you with a game
that refuses to launch and nothing on the page explaining why.

Being listed is not the same as being needed. All five of NooDS's files are optional, so none of them turns a GBA page
red by itself — and what each row means for the core you actually launch with is the next section's question.

## Active Core Detection

Different emulator cores can have different BIOS requirements for the same platform. The plugin detects which core
RetroDECK is actually configured to use and filters the BIOS list accordingly, so you only see the files that matter for
your setup.

### Example: Game Boy Advance

- With **mGBA** (RetroDECK's default), `gba_bios.bin` is shown as _optional_ — mGBA has a built-in high-level BIOS
  replacement
- With **gpSP**, `gba_bios.bin` is shown as _required_ — gpSP cannot run without it

The active core name appears on the game detail page (the **Emulator** column) and on the platform's header line in
**Library › Platforms**. This tells you at a glance which core the plugin is filtering for.

**How the core is determined:**

1. If you set a **per-game core** for this game in the plugin, that wins. (Per-game cores are stored by the plugin
   itself — see [Per-Game (Game Detail Page)](#per-game-game-detail-page) below.)
2. If no per-game core, the plugin checks for a **per-platform core** you set in Library › Platforms — stored by the
   plugin in its own settings, not in ES-DE.
3. The plugin reads RetroDECK's ES-DE configuration (`es_systems.xml`) from the flatpak installation to find the default
   emulator for each platform — for BIOS filtering it uses the platform's first RetroArch core. This live file is the
   only source; there is no bundled fallback snapshot.
4. If a platform offers **only standalone emulators** (no RetroArch core at all), or the live configuration can't be
   read, the plugin has nothing to filter with — so it does not filter, and it does not guess either. Every BIOS file
   the platform has is listed, each marked _unknown_, and the platform's summary reads **BIOS requirement unknown** with
   a grey dot and no download buttons — including when the platform has no files to list, which is where saying nothing
   at all would have read as "nothing needed". That is the honest answer for a platform like PS3, whose emulators the
   plugin cannot yet ask: saying nothing is needed would report it ready over firmware the emulator will not boot
   without.

Whatever this chain resolves to is the **same core the game launches on** — the plugin bakes the resolved core into the
Steam shortcut, so the core shown for BIOS, saves, and the core badge always matches the core that runs.

The detection chain ensures BIOS filtering works even when RetroDECK's configuration files aren't accessible (e.g. after
an update changes paths). You'll see a "Core: mGBA" badge when detection is working, or no badge when falling back to
showing all files.

## Changing the Active Core

You can change the active emulator core directly from the plugin, without leaving Game Mode. There are two scopes, and
**both are stored by the plugin itself** — neither touches ES-DE's `gamelist.xml`. The plugin bakes the chosen core
directly into each game's Steam shortcut, so your choice applies reliably for any ROM filename.

- **Per-platform** changes set the core for every game on a platform. Stored in the plugin's own settings.
- **Per-game** changes set the core for a single game and take priority over the platform choice. Stored by the plugin
  on the game, so they survive uninstalling and re-downloading.

### Per-Platform (Library Platforms tab)

In **Library › Platforms**, a platform with more than one emulator shows a **Change core** button under its header line.
The button opens a menu listing every emulator ES-DE offers for that platform — both RetroArch cores and **standalone
emulators** (e.g. PCSX2, RPCS3, Dolphin, PPSSPP). Some entries appear **disabled** with a short reason (for example
"script/shortcut form" or "needs setup files (launch via ES-DE once)") when the plugin can't launch them directly from
Steam; those can't be picked. Picking an enabled emulator sets it as the default for all games on that platform. A
"Switching cores may affect save compatibility" note appears at the top of the menu and under the button.

1. Open **Library** from the main QAM page and move to the **Platforms** tab
2. Move to the platform you want to change
3. Press **Change core** and pick an emulator from the menu
4. The BIOS table below updates to show what the new choice needs

A platform that offers one emulator says so instead of showing a button. If RetroDECK can't be found (no
`es_systems.xml`), the pane says the emulator list is unavailable rather than showing an empty picker.

The plugin stores the choice in its own settings and **immediately re-applies it** to every installed game on that
platform — the change takes effect right away, with no sync needed (games that already have a per-game core keep their
own choice). If the switch cannot be made, the pane says so under the button and the header keeps naming the core that
is actually in effect. The page works even when your RomM server is offline — core switching and BIOS status are
available, only the download buttons are withdrawn.

!!! note "A RetroDECK default-core change needs a Force Full Sync"

    Setting a per-platform core here re-bakes your installed games right away. But if a **RetroDECK
    update** ships a _new default core_ for a platform (and you have not picked a core yourself), that new default does
    **not** take effect on a normal sync — a normal sync skips platforms whose games haven't changed, so the
    previously-baked core stays. Run a **Force Full Sync** to re-bake every game and pick up RetroDECK's new default.

### Per-Game (Game Detail Page)

On the game detail page, a **CPU button** (microchip icon) appears between the RomM and Steam gear buttons when the
game's platform offers more than one emulator. The menu lists the same emulators as the platform pane — RetroArch cores
and **standalone emulators** — and shows the ones the plugin can't launch from Steam as **disabled** with a short
reason.

1. Open a game's detail page
2. Tap the **CPU button** (microchip icon)
3. Pick an emulator from the menu, or the **Use System Override** item at the top (see below)
4. The BIOS status, core badge, and game info panel update immediately

At the top of the menu, above the core list, is a dedicated **Use System Override (X)** item. Selecting it **clears**
the per-game core so the game follows whatever the system would pick — the per-platform core you set in Library ›
Platforms, or the platform's default core when no per-platform override is set. **X** is that fallback core's name,
shown in parentheses so you know what the game will fall back to.

Each core in the list below can show up to three markers, one per role:

- **(default)** — the RetroDECK/es_systems default core for this platform.
- **(system)** — the per-platform core you picked in [Library › Platforms](#per-platform-library-platforms-tab) (stored
  in the plugin's settings). Absent when the platform has no per-platform override.
- **✓** (checkmark) — the core this game actually launches with right now.

The three roles are independent, so a single core can carry more than one marker: "(default) (system)" when your
per-platform pick happens to equal the default, or "(system) ✓" when the per-platform core is also the one the game
launches with.

A per-game core takes priority over the platform default. **Every core in the list pins** when you pick it — including
the one marked **(default)**. Pinning the default-marked core fixes the game to that specific core even if you later
change the per-platform override; it is no longer the way to "follow the system".

To drop the per-game core and follow the platform/system core again, pick the **Use System Override** item at the top —
that is the only thing that clears the per-game override. The **✓** can appear in two places at once: when the game is
following the system (no per-game core), the **Use System Override** item carries the ✓ **and** so does the core that is
actually in effect. When you pin a per-game core, only that pinned core carries the ✓ and the **Use System Override**
item does not.

When you set or reset a per-game core for an installed game, the plugin updates the game's Steam shortcut immediately
and confirms the change landed before reporting success. If Steam can't accept the change in the current session, you'll
see a "Core saved — restart Steam to apply" message — your choice is still saved; it takes effect after a Steam restart
(or the next sync).

Per-game cores work for **any ROM filename**. The plugin bakes the chosen core directly into the game's launch command,
so it does not rely on RetroDECK's gamelist lookup (which mishandles parentheses and other special characters in
filenames) and is not affected by that upstream limitation.

### Core choices are not migrated from ES-DE

The plugin now owns core selection entirely and no longer reads or writes ES-DE's `gamelist.xml`. A few notes for anyone
upgrading from an older build or who edits ES-DE directly:

- **Per-platform cores set in ES-DE are not carried over — re-apply them once.** Earlier builds stored a per-system core
  as a `<alternativeEmulator>` in ES-DE's `gamelist.xml`; the plugin now stores per-platform cores in its own settings
  and does **not** read or import that ES-DE entry. If you had set a per-system core, re-apply it once in **Library ›
  Platforms** (the Emulator Core button/menu) and it sticks from then on.
- **Per-game cores set with an older plugin build are not carried over.** Earlier builds stored per-game cores in
  ES-DE's `gamelist.xml`; the plugin now stores them itself and does not import the old entries. Re-apply any per-game
  core once through the CPU-button menu and it sticks from then on (including across uninstall/re-download).
- **A core set directly in ES-DE is not seen by the plugin.** If you pick a core for a game (or a system) in ES-DE's own
  interface, the plugin's BIOS badge, per-core save path, and core-change warning will **not** reflect it — those follow
  the core the plugin knows about, and the plugin's launches always use the core it has baked in. ES-DE-native launches
  still honour your ES-DE setting. To keep the plugin's badges, save paths, and launches in sync, set the core through
  the plugin (the CPU-button menu for one game, Library › Platforms for a whole platform) instead.

### Non-Default Core Indicator

The CPU button changes color to indicate the active core status:

- **Gray** — the default core is active (no overrides)
- **Yellow** — a non-default core is active (per-game or per-platform override)

The game detail info panel shows the active core in a dedicated "Emulator" column alongside the BIOS status, using a
two-column layout.

---

**Previous:** [Managing Games](managing-games.md) | **Next:** [RetroDECK Path Migration](retrodeck-path-migration.md)
