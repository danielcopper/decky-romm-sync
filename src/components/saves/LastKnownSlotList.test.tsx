import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { renderLastKnownSlots } from "./LastKnownSlotList";
import type { LastKnownSlots, SaveSlotSummary } from "../../types";

function makeSlot(overrides: Partial<SaveSlotSummary> = {}): SaveSlotSummary {
  return {
    slot: "main",
    source: "server",
    count: 1,
    latest_updated_at: null,
    ...overrides,
  };
}

function makeLastKnown(overrides: Partial<LastKnownSlots> = {}): LastKnownSlots {
  return {
    slots: [makeSlot()],
    activeSlot: "main",
    ...overrides,
  };
}

describe("renderLastKnownSlots", () => {
  it("lists every slot with its source and save count", () => {
    const { getByTestId } = render(
      renderLastKnownSlots(
        makeLastKnown({
          slots: [makeSlot({ slot: "main", source: "server", count: 3 }), makeSlot({ slot: "solo", source: "local" })],
        }),
      ),
    );
    expect(getByTestId("last-known-slot-main").textContent).toContain("main");
    expect(getByTestId("last-known-slot-main").textContent).toContain("server");
    expect(getByTestId("last-known-slot-main").textContent).toContain("3 saves");
    expect(getByTestId("last-known-slot-solo").textContent).toContain("local");
    expect(getByTestId("last-known-slot-solo").textContent).toContain("1 save");
  });

  it("marks the slot that was active", () => {
    const { getByTestId } = render(
      renderLastKnownSlots(
        makeLastKnown({ slots: [makeSlot({ slot: "main" }), makeSlot({ slot: "other" })], activeSlot: "main" }),
      ),
    );
    expect(getByTestId("last-known-slot-main").textContent).toContain("active");
    expect(getByTestId("last-known-slot-other").textContent).not.toContain("active");
  });

  it("marks the legacy bucket active when the snapshot's active slot is null", () => {
    const { getByTestId } = render(
      renderLastKnownSlots(makeLastKnown({ slots: [makeSlot({ slot: "" })], activeSlot: null })),
    );
    const row = getByTestId("last-known-slot-legacy");
    expect(row.textContent).toContain("Legacy");
    expect(row.textContent).toContain("active");
  });

  it("says the numbers are RomM's last answer rather than the current ones", () => {
    const { container } = render(renderLastKnownSlots(makeLastKnown()));
    expect(container.textContent).toContain("Slots as RomM last reported them");
    expect(container.textContent).toContain("counts and times are from that answer, not from now");
  });

  it("dates the note with nothing — no timestamp stands for the snapshot's age (#1755)", () => {
    // The only timestamp the device has moves with syncs and slot switches, so
    // any date here would be a claim about the numbers below that isn't true.
    const { container } = render(
      renderLastKnownSlots(makeLastKnown({ slots: [makeSlot({ latest_updated_at: null })] })),
    );
    const note = container.querySelector(".romm-slot-stale-note")?.textContent ?? "";
    expect(note).not.toMatch(/\d/);
    expect(note).not.toContain("ago");
  });

  it("shows a slot's newest save only when the snapshot recorded one", () => {
    const { getByTestId } = render(
      renderLastKnownSlots(
        makeLastKnown({
          slots: [
            makeSlot({ slot: "main", latest_updated_at: "2026-04-17T10:00:00Z" }),
            makeSlot({ slot: "empty", latest_updated_at: null }),
          ],
        }),
      ),
    );
    expect(getByTestId("last-known-slot-main").textContent).toContain("Newest save:");
    expect(getByTestId("last-known-slot-empty").textContent).not.toContain("Newest save:");
  });

  it("renders nothing pressable", () => {
    const { container } = render(renderLastKnownSlots(makeLastKnown()));
    expect(container.querySelectorAll("button")).toHaveLength(0);
  });
});
