import { describe, it, expect, beforeEach, vi } from "vitest";
import { getPlaytimeScopeNotice } from "../api/backend";
import {
  getPlaytimeScopeState,
  setPlaytimeScopeState,
  onPlaytimeScopeChange,
  fetchPlaytimeScopeState,
} from "./playtimeScopeStore";

describe("playtimeScopeStore", () => {
  beforeEach(() => {
    setPlaytimeScopeState({ pending: false });
    vi.mocked(getPlaytimeScopeNotice).mockReset();
  });

  it("starts not-pending", () => {
    expect(getPlaytimeScopeState()).toEqual({ pending: false });
  });

  it("setPlaytimeScopeState updates the state and notifies subscribers", () => {
    const fn = vi.fn();
    onPlaytimeScopeChange(fn);
    setPlaytimeScopeState({ pending: true });
    expect(getPlaytimeScopeState()).toEqual({ pending: true });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("onPlaytimeScopeChange returns an unsubscribe that stops notifications", () => {
    const fn = vi.fn();
    const unsub = onPlaytimeScopeChange(fn);
    unsub();
    setPlaytimeScopeState({ pending: true });
    expect(fn).not.toHaveBeenCalled();
  });

  it("fetchPlaytimeScopeState maps the backend shape and updates the store", async () => {
    vi.mocked(getPlaytimeScopeNotice).mockResolvedValue({ pending: true });
    const result = await fetchPlaytimeScopeState();
    expect(result).toEqual({ pending: true });
    expect(getPlaytimeScopeState()).toEqual({ pending: true });
  });

  it("fetchPlaytimeScopeState clears the store when the backend reports not-pending", async () => {
    setPlaytimeScopeState({ pending: true });
    vi.mocked(getPlaytimeScopeNotice).mockResolvedValue({ pending: false });
    const result = await fetchPlaytimeScopeState();
    expect(result).toEqual({ pending: false });
    expect(getPlaytimeScopeState()).toEqual({ pending: false });
  });
});
