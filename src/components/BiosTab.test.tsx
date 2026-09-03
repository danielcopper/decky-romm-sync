// The pane's content is asserted through the panel that owns every field it
// renders (RomMGameInfoPanel.test.tsx). What this file exists for is the
// mounting contract: the panel mounts this tab for every ROM and leaves it
// mounted, so rendering has to be gated on `isActive` — a panel test cannot
// tell "the pane rendered nothing" apart from "the pane was never mounted".

import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { BiosTab } from "./BiosTab";
import type { BiosStatus, CoreInfo } from "../types";

const coreInfo: CoreInfo = {
  active_core: "snes9x_libretro",
  active_core_label: "Snes9x",
  platform_core_label: null,
  has_game_override: false,
  emulator_data_available: true,
  emulators: [
    { label: "Snes9x", kind: "libretro", core_so: "snes9x_libretro", is_default: true, bakeable: true, reason: null },
  ],
};

// A state the backend can actually emit: the "missing" level comes from a
// required file that is absent, so the required counts have to be there. Paired
// with no `required_count` it would be unreachable — a zero required count
// always computes "ok".
const biosStatus: BiosStatus = {
  needs_bios: true,
  server_count: 1,
  local_count: 0,
  all_downloaded: false,
  required_count: 1,
  required_downloaded: 0,
};

describe("BiosTab", () => {
  it("renders the requirement and its active core when it is the active tab", () => {
    const { container } = render(
      <BiosTab biosStatus={biosStatus} biosLevel="missing" coreInfo={coreInfo} isActive={true} />,
    );
    expect(container.textContent).toContain("0/1 required files ready");
    expect(container.textContent).toContain("Snes9x");
  });

  it("renders nothing while another tab is showing", () => {
    const { container } = render(
      <BiosTab biosStatus={biosStatus} biosLevel="missing" coreInfo={coreInfo} isActive={false} />,
    );
    expect(container.textContent).toBe("");
  });

  it("renders nothing when nothing needs BIOS", () => {
    const { container } = render(<BiosTab biosStatus={null} biosLevel={null} coreInfo={coreInfo} isActive={true} />);
    expect(container.textContent).toBe("");
  });

  it("renders the unknown reading off a status with no counts at all", () => {
    // What a platform whose emulators cannot be asked hands the tab: the wire
    // payload for "nothing could establish it", with no file rows and no
    // aggregates to read. The pane still has to say so rather than fall through
    // to the no-requirement sentence, whose counts would both be zero.
    const { container } = render(
      <BiosTab
        biosStatus={{ needs_bios: false, bios_status_unknown: true }}
        biosLevel="unknown"
        coreInfo={coreInfo}
        isActive={true}
      />,
    );
    expect(container.textContent).toContain("BIOS requirement unknown");
    expect(container.textContent).not.toContain("Nothing required");
    expect(container.innerHTML).toContain("#8f98a0");
  });

  it("drops the ratio when the library holds none of the platform's files", () => {
    // "Nothing required (0/0 files held)" counts a set that does not exist.
    const { container } = render(
      <BiosTab
        biosStatus={{ needs_bios: true, server_count: 0, local_count: 0, all_downloaded: false, required_count: 0 }}
        biosLevel="ok"
        coreInfo={coreInfo}
        isActive={true}
      />,
    );
    expect(container.textContent).toContain("Nothing required");
    expect(container.textContent).not.toContain("files held");
  });

  it("draws a folder row whose contents could not be read amber, never green", () => {
    // The colours are the pane's own, so they are pinned here rather than
    // through the panel: `downloaded` is true for a folder — something is at the
    // destination — and green over a verdict nothing established is the false
    // all-clear this row exists to avoid. No core matches `coreInfo`'s active
    // one, so the amber cannot have come from the active-core highlight.
    const { container } = render(
      <BiosTab
        biosStatus={{
          needs_bios: true,
          server_count: 0,
          local_count: 0,
          all_downloaded: false,
          required_count: 1,
          required_downloaded: 0,
          required_withheld: 1,
          files: [
            {
              file_name: "bios",
              downloaded: true,
              local_path: "",
              description: "'pcsx2/bios' folder",
              wanted: "needed",
              required_by_active: true,
              cores: {},
              on_server: false,
              declared_kind: "directory",
              satisfied: null,
              caveats: ["firmware-scan-incomplete"],
            },
          ],
        }}
        biosLevel="unknown"
        coreInfo={coreInfo}
        isActive={true}
      />,
    );
    expect(container.innerHTML).toContain("#d4a72c");
    expect(container.innerHTML).not.toContain("#5ba32b");
  });

  it.each([
    ["green over a folder holding an image", true, "#5ba32b", "#d94126", ["firmware-image-identified"], ["Europe"]],
    ["red over a folder holding none", false, "#d94126", "#5ba32b", ["firmware-directory-holds-no-image"], []],
  ] as const)("draws %s", (_name, satisfied, colour, otherColour, caveats, images) => {
    // The folder's verdict is what it HOLDS, so it takes the same two colours a
    // declared file does — the amber above is for a verdict, not for a folder.
    //
    // Asserted on the file row alone, because the header dot beside it emits the
    // same hex for these two levels: over the whole container the presence check
    // would pass on the header without the row's dot ever being drawn.
    const { container } = render(
      <BiosTab
        biosStatus={{
          needs_bios: true,
          server_count: 0,
          local_count: 0,
          all_downloaded: false,
          required_count: 1,
          required_downloaded: satisfied ? 1 : 0,
          required_withheld: 0,
          files: [
            {
              file_name: "bios",
              downloaded: true,
              local_path: "",
              description: "'pcsx2/bios' folder",
              wanted: "needed",
              required_by_active: true,
              cores: {},
              on_server: false,
              declared_kind: "directory",
              satisfied,
              caveats: [...caveats],
              images: [...images],
            },
          ],
        }}
        biosLevel={satisfied ? "ok" : "missing"}
        coreInfo={coreInfo}
        isActive={true}
      />,
    );
    const row = container.querySelector(".romm-panel-file-row");
    expect(row?.innerHTML).toContain(colour);
    expect(row?.innerHTML).not.toContain(otherColour);
    expect(row?.innerHTML).not.toContain("#d4a72c");
  });

  it("falls back to 'Default' when no active core is resolved", () => {
    const { container } = render(
      <BiosTab
        biosStatus={biosStatus}
        biosLevel="missing"
        coreInfo={{ ...coreInfo, active_core: null, active_core_label: null }}
        isActive={true}
      />,
    );
    expect(container.textContent).toContain("Default");
  });
});
