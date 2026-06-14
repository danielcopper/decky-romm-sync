import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { SettingsResetBanner, settingsResetMessage, SETTINGS_RESET_TITLE } from "./SettingsResetBanner";
import * as backend from "../api/backend";
import { getSettingsResetState, setSettingsResetState } from "../utils/settingsResetStore";

vi.mock("../api/backend", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/backend")>();
  return {
    ...actual,
    dismissSettingsResetNotice: vi.fn(),
    logError: vi.fn(),
  };
});

const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

describe("settingsResetMessage", () => {
  it("names the backup file when known", () => {
    const msg = settingsResetMessage("settings.json.corrupt-42");
    expect(msg).toContain("Re-enter your server URL and sign in again");
    expect(msg).toContain("settings.json.corrupt-42");
  });

  it("falls back to a generic backup line when the path is null", () => {
    const msg = settingsResetMessage(null);
    expect(msg).toContain("A backup of your old settings was saved.");
    expect(msg).not.toContain("corrupt-");
  });
});

describe("SettingsResetBanner component", () => {
  beforeEach(() => {
    vi.mocked(backend.dismissSettingsResetNotice).mockReset();
    vi.mocked(backend.logError).mockReset();
    setSettingsResetState({ pending: true, backedUpTo: "settings.json.corrupt-seed" });
  });

  it("renders the PanelSection title + the message naming the backup", () => {
    const { container } = render(<SettingsResetBanner backedUpTo="settings.json.corrupt-1781697600" />);
    // PanelSection's `title` prop is forwarded by the global stub as a DOM
    // attribute on <section>, so assert via getAttribute, not textContent.
    const section = container.querySelector("section");
    expect(section?.getAttribute("title")).toBe(SETTINGS_RESET_TITLE);
    expect(container.textContent).toContain(SETTINGS_RESET_TITLE);
    expect(container.textContent).toContain("Re-enter your server URL and sign in again");
    expect(container.textContent).toContain("settings.json.corrupt-1781697600");
  });

  it("renders the generic backup line when backedUpTo is null", () => {
    const { container } = render(<SettingsResetBanner backedUpTo={null} />);
    expect(container.textContent).toContain("A backup of your old settings was saved.");
  });

  it("renders a Dismiss button", () => {
    const { getByText } = render(<SettingsResetBanner backedUpTo="settings.json.corrupt-9" />);
    expect(getByText("Dismiss")).toBeInTheDocument();
  });

  it("Dismiss → calls dismissSettingsResetNotice and clears the shared store on success", async () => {
    vi.mocked(backend.dismissSettingsResetNotice).mockResolvedValue({ success: true });
    const { getByText } = render(<SettingsResetBanner backedUpTo="settings.json.corrupt-9" />);
    await act(async () => {
      fireEvent.click(getByText("Dismiss"));
      await flushAsync();
    });
    expect(backend.dismissSettingsResetNotice).toHaveBeenCalledTimes(1);
    // Store flips to not-pending → banner + every game-detail card disappear.
    expect(getSettingsResetState()).toEqual({ pending: false, backedUpTo: null });
  });

  it("Dismiss rejection → logs the error and leaves the store pending (banner stays up)", async () => {
    vi.mocked(backend.dismissSettingsResetNotice).mockRejectedValue(new Error("disk full"));
    const { getByText } = render(<SettingsResetBanner backedUpTo="settings.json.corrupt-9" />);
    await act(async () => {
      fireEvent.click(getByText("Dismiss"));
      await flushAsync();
    });
    // CATCH-REJECTION rule: assert the post-catch side effect (the logError).
    expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("Failed to dismiss settings reset notice"));
    // Store untouched — still pending so the banner remains.
    expect(getSettingsResetState()).toEqual({ pending: true, backedUpTo: "settings.json.corrupt-seed" });
  });
});
