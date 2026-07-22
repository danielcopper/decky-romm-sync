import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { CopyToSlotModal } from "./CopyToSlotModal";
import type { SaveSlotSummary } from "../../types";

function slot(name: string): SaveSlotSummary {
  return { slot: name, source: "server", count: 1, latest_updated_at: null };
}

const SLOTS: SaveSlotSummary[] = [slot("autosave"), slot("backup"), slot(""), slot("promoted")];

function renderModal(overrides: { sourceSlot?: string; availableSlots?: SaveSlotSummary[] } = {}) {
  const onSubmit = vi.fn();
  const closeModal = vi.fn();
  const utils = render(
    <CopyToSlotModal
      availableSlots={overrides.availableSlots ?? SLOTS}
      sourceSlot={overrides.sourceSlot ?? "backup"}
      onSubmit={onSubmit}
      closeModal={closeModal}
    />,
  );
  return { ...utils, onSubmit, closeModal };
}

describe("CopyToSlotModal", () => {
  it("lists existing named slots as targets, excluding the source and the legacy bucket", () => {
    const { getByText, queryByText } = renderModal({ sourceSlot: "backup" });
    // Named siblings are offered…
    expect(getByText("autosave")).toBeInTheDocument();
    expect(getByText("promoted")).toBeInTheDocument();
    // …the source slot is not (same-slot copy == rollback)…
    expect(queryByText("backup")).toBeNull();
    // …and the legacy "" bucket (displayed as "Legacy") is a read-only source only.
    expect(queryByText("Legacy")).toBeNull();
  });

  it("submits an existing target and closes on pick", () => {
    const { getByText, onSubmit, closeModal } = renderModal({ sourceSlot: "backup" });
    fireEvent.click(getByText("autosave"));
    expect(onSubmit).toHaveBeenCalledWith("autosave");
    expect(closeModal).toHaveBeenCalled();
  });

  it("creates and copies into a new slot with the trimmed name", () => {
    const { getByTestId, getByText, onSubmit, closeModal } = renderModal();
    fireEvent.change(getByTestId("text-field"), { target: { value: "  speedrun  " } });
    fireEvent.click(getByText("Create & copy here"));
    expect(onSubmit).toHaveBeenCalledWith("speedrun");
    expect(closeModal).toHaveBeenCalled();
  });

  it("submits the new-slot name on Enter (the on-screen keyboard's Eingabe key)", () => {
    const { getByTestId, onSubmit, closeModal } = renderModal();
    const field = getByTestId("text-field");
    fireEvent.change(field, { target: { value: "  speedrun  " } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("speedrun");
    expect(closeModal).toHaveBeenCalled();
  });

  it("ignores Enter while the new-slot field is empty (no onSubmit)", () => {
    const { getByTestId, onSubmit, closeModal } = renderModal();
    fireEvent.keyDown(getByTestId("text-field"), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(closeModal).not.toHaveBeenCalled();
  });

  it("disables Create while the new-slot field is empty or whitespace", () => {
    const { getByText, getByTestId } = renderModal();
    const createBtn = getByText("Create & copy here").closest("button");
    expect(createBtn).toBeDisabled();
    fireEvent.change(getByTestId("text-field"), { target: { value: "   " } });
    expect(createBtn).toBeDisabled();
  });

  it("rejects an empty new-slot submission (no onSubmit)", () => {
    const { getByText, onSubmit } = renderModal();
    // The button is disabled, so a click never reaches the empty-guarded pick().
    fireEvent.click(getByText("Create & copy here"));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("shows a hint when there are no other slots to copy into", () => {
    const { getByText } = renderModal({ sourceSlot: "backup", availableSlots: [slot("backup"), slot("")] });
    expect(getByText("No other slots yet — create one below.")).toBeInTheDocument();
  });
});
