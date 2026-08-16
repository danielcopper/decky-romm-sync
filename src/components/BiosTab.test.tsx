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

const biosStatus: BiosStatus = {
  needs_bios: true,
  server_count: 1,
  local_count: 0,
  all_downloaded: false,
};

describe("BiosTab", () => {
  it("renders the requirement and its active core when it is the active tab", () => {
    const { container } = render(
      <BiosTab biosStatus={biosStatus} biosLevel="missing" coreInfo={coreInfo} isActive={true} />,
    );
    expect(container.textContent).toContain("0/1 files ready");
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
