// The one place the two BIOS surfaces get their row wording from, so the cases
// are pinned here once rather than twice in two component suites. What each
// surface does with the result — suffix, description, or nothing — is pinned
// with that surface.

import { describe, it, expect } from "vitest";
import { biosFileNote } from "./biosFileNote";

/** LRPS2's `pcsx2/bios` row, with the verdict a test wants to word. */
const folder = (satisfied: boolean | null, caveats: string[] = [], images: string[] = []) => ({
  downloaded: true,
  on_server: false,
  declared_kind: "directory" as const,
  satisfied,
  caveats,
  images,
});

describe("biosFileNote", () => {
  it("says nothing about a plain library file, present or missing", () => {
    // Its state is the row's own to show; the surfaces disagree on how, so the
    // shared note stays out of it.
    expect(biosFileNote({ downloaded: true, on_server: true, satisfied: true })).toBe("");
    expect(biosFileNote({ downloaded: false, on_server: true, satisfied: false })).toBe("");
  });

  it("names the distribution that supplied the file", () => {
    // Verbatim: the name is the resolver's own display form for the
    // distribution, so anything prettier here would be a name we invented.
    expect(biosFileNote({ downloaded: true, on_server: false, supplied_by: "RetroDECK" })).toBe(
      "provided by RetroDECK",
    );
  });

  it("prefers the distribution over the library note", () => {
    // "not in your RomM library" is true of codehandler.bin and useless: no
    // library will ever hold it, and the emulator already did put it there.
    expect(biosFileNote({ downloaded: true, on_server: false, supplied_by: "RetroDECK" })).not.toContain("RomM");
  });

  it("lists the images a satisfied folder holds, in the resolver's own words", () => {
    // The core needs exactly one of them, so none of them is labelled required
    // or optional and none is marked as the one that will load — which of them
    // LRPS2 picks is a core option this plugin does not read.
    expect(
      biosFileNote(
        folder(true, ["firmware-image-identified"], ["Europe  v02.00(14/06/2004)", "Japan   v02.00(14/06/2004)"]),
      ),
    ).toBe("holds Europe  v02.00(14/06/2004), Japan   v02.00(14/06/2004)");
  });

  it("still says a satisfied folder holds an image when none was named", () => {
    expect(biosFileNote(folder(true))).toBe("holds a BIOS image");
  });

  it("says an unmet folder holds no image", () => {
    expect(biosFileNote(folder(false, ["firmware-directory-holds-no-image"]))).toBe("holds no BIOS image");
    expect(biosFileNote(folder(false, ["firmware-directory-holds-no-candidate"]))).toBe("holds no BIOS image");
  });

  it("leaves an absent folder to the surfaces' own word for missing", () => {
    // Nothing was listed, so there is no finding about contents to report —
    // the row is simply not there, like any other.
    expect(biosFileNote({ ...folder(false), downloaded: false })).toBe("");
  });

  it("words each withheld folder verdict off its own code", () => {
    expect(biosFileNote(folder(null, ["firmware-image-contradicted"]))).toBe(
      "holds an image that could not be confirmed",
    );
    expect(biosFileNote(folder(null, ["firmware-scan-incomplete"]))).toBe("its contents could not be read in full");
    expect(biosFileNote(folder(null, ["firmware-unreadable"]))).toBe("its contents could not be read in full");
    // No code at all is what a read that failed and a question nobody asked
    // both leave behind — the payload cannot tell them apart, and the fallback
    // is written not to try.
    expect(biosFileNote(folder(null))).toBe("its contents could not be checked");
  });

  it("says the wrong shape is at a destination, in both directions", () => {
    expect(biosFileNote({ ...folder(false), caveats: ["firmware-path-not-a-directory"] })).toBe(
      "a file is here, where the emulator opens a folder",
    );
    expect(
      biosFileNote({
        downloaded: true,
        on_server: true,
        declared_kind: "file",
        satisfied: null,
        caveats: ["firmware-path-obstructed"],
      }),
    ).toBe("a folder is here, where the emulator opens a file");
  });

  it("says a file's destination could not be read, rather than leaving it to read as absent", () => {
    // The row is red and counted unmet either way — what the plugin cannot read
    // the emulator cannot open — so the note says the READ failed and claims
    // nothing about whether the file is there.
    expect(
      biosFileNote({
        downloaded: false,
        on_server: true,
        declared_kind: "file",
        satisfied: false,
        caveats: ["firmware-path-inaccessible"],
      }),
    ).toBe("its location could not be read");
  });

  it("leaves an unreadable folder to the withheld wording it already has", () => {
    // The same code on a folder row, whose verdict is withheld rather than
    // unmet — the two halves say the read failed in their own words.
    expect(biosFileNote(folder(null, ["firmware-path-inaccessible"]))).toBe("its contents could not be checked");
  });

  it("keeps the library note for a row nothing else was established for", () => {
    expect(biosFileNote({ downloaded: true, on_server: false, satisfied: true })).toBe("not in your RomM library");
    expect(biosFileNote({ downloaded: false, on_server: false, satisfied: false })).toBe(
      "missing, not in your RomM library",
    );
  });
});
