// The one place the two BIOS surfaces get their row wording from, so the cases
// are pinned here once rather than twice in two component suites. What each
// surface does with the result — suffix, description, or nothing — is pinned
// with that surface.

import { describe, it, expect } from "vitest";
import { biosFileNote } from "./biosFileNote";

describe("biosFileNote", () => {
  it("says nothing about a plain library file, present or missing", () => {
    // Its state is the row's own to show; the surfaces disagree on how, so the
    // shared note stays out of it.
    expect(biosFileNote({ downloaded: true, on_server: true })).toBe("");
    expect(biosFileNote({ downloaded: false, on_server: true })).toBe("");
  });

  it("names the distribution that supplied the file", () => {
    // Verbatim: the resolver states an identifier and no display form, so
    // anything prettier here would be a name the plugin invented.
    expect(biosFileNote({ downloaded: true, on_server: false, supplied_by: "retrodeck" })).toBe(
      "provided by retrodeck",
    );
  });

  it("prefers the distribution over the library note", () => {
    // "not in your RomM library" is true of codehandler.bin and useless: no
    // library will ever hold it, and the emulator already did put it there.
    expect(biosFileNote({ downloaded: true, on_server: false, supplied_by: "retrodeck" })).not.toContain("RomM");
  });

  it("says what a directory requirement is satisfied by", () => {
    // LRPS2 declares `pcsx2/bios`, a folder — "missing, not in your RomM
    // library" read as though the folder itself were the file to fetch.
    expect(biosFileNote({ downloaded: true, on_server: false, is_directory: true })).toBe(
      "BIOS files go in this folder",
    );
  });

  it("keeps the library note for a row nothing else was established for", () => {
    expect(biosFileNote({ downloaded: true, on_server: false })).toBe("not in your RomM library");
    expect(biosFileNote({ downloaded: false, on_server: false })).toBe("missing, not in your RomM library");
  });
});
