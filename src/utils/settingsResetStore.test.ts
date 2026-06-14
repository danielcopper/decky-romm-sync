import { describe, it, expect, beforeEach, vi } from "vitest";
import { getSettingsResetNotice } from "../api/backend";
import {
  getSettingsResetState,
  setSettingsResetState,
  onSettingsResetChange,
  fetchSettingsResetState,
} from "./settingsResetStore";

describe("settingsResetStore", () => {
  beforeEach(() => {
    setSettingsResetState({ pending: false, backedUpTo: null });
    vi.mocked(getSettingsResetNotice).mockReset();
  });

  it("starts not-pending", () => {
    expect(getSettingsResetState()).toEqual({ pending: false, backedUpTo: null });
  });

  it("setSettingsResetState updates the state and notifies subscribers", () => {
    const fn = vi.fn();
    onSettingsResetChange(fn);
    setSettingsResetState({ pending: true, backedUpTo: "settings.json.corrupt-7" });
    expect(getSettingsResetState()).toEqual({ pending: true, backedUpTo: "settings.json.corrupt-7" });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("onSettingsResetChange returns an unsubscribe that stops notifications", () => {
    const fn = vi.fn();
    const unsub = onSettingsResetChange(fn);
    unsub();
    setSettingsResetState({ pending: true, backedUpTo: null });
    expect(fn).not.toHaveBeenCalled();
  });

  it("fetchSettingsResetState maps the backend shape and updates the store", async () => {
    vi.mocked(getSettingsResetNotice).mockResolvedValue({
      pending: true,
      backed_up_to: "settings.json.corrupt-42",
    });
    const result = await fetchSettingsResetState();
    expect(result).toEqual({ pending: true, backedUpTo: "settings.json.corrupt-42" });
    expect(getSettingsResetState()).toEqual({ pending: true, backedUpTo: "settings.json.corrupt-42" });
  });

  it("fetchSettingsResetState clears the store when the backend reports not-pending", async () => {
    setSettingsResetState({ pending: true, backedUpTo: "settings.json.corrupt-old" });
    vi.mocked(getSettingsResetNotice).mockResolvedValue({ pending: false, backed_up_to: null });
    const result = await fetchSettingsResetState();
    expect(result).toEqual({ pending: false, backedUpTo: null });
    expect(getSettingsResetState()).toEqual({ pending: false, backedUpTo: null });
  });
});
