import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { useEffect, useState, FC } from "react";
import { PlaytimeScopeBanner, PLAYTIME_SCOPE_TITLE, PLAYTIME_SCOPE_MESSAGE } from "./PlaytimeScopeBanner";
import { getPlaytimeScopeNotice } from "../api/backend";
import {
  getPlaytimeScopeState,
  setPlaytimeScopeState,
  onPlaytimeScopeChange,
  fetchPlaytimeScopeState,
} from "../utils/playtimeScopeStore";

const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

/**
 * Minimal harness mirroring MainPage's `{playtimeScope.pending && <Banner/>}`
 * conditional + its mount fetch, so the callable → store → render pipeline is
 * exercised end-to-end without mounting all of MainPage's unrelated callables.
 */
const ScopeBannerHost: FC = () => {
  const [scope, setScope] = useState(getPlaytimeScopeState());
  useEffect(() => {
    fetchPlaytimeScopeState().catch(() => {});
    const unsub = onPlaytimeScopeChange(() => setScope(getPlaytimeScopeState()));
    return unsub;
  }, []);
  return scope.pending ? <PlaytimeScopeBanner /> : null;
};

describe("PlaytimeScopeBanner component", () => {
  beforeEach(() => {
    vi.mocked(getPlaytimeScopeNotice).mockReset();
    setPlaytimeScopeState({ pending: false });
  });

  it("renders the PanelSection title + the sign-in message", () => {
    const { container } = render(<PlaytimeScopeBanner />);
    // PanelSection's `title` prop is forwarded by the global stub as a DOM
    // attribute on <section>, so assert via getAttribute, not textContent.
    const section = container.querySelector("section");
    expect(section?.getAttribute("title")).toBe(PLAYTIME_SCOPE_TITLE);
    expect(container.textContent).toContain(PLAYTIME_SCOPE_TITLE);
    expect(container.textContent).toContain(PLAYTIME_SCOPE_MESSAGE);
  });

  it("renders a Dismiss button", () => {
    const { getByText } = render(<PlaytimeScopeBanner />);
    expect(getByText("Dismiss")).toBeInTheDocument();
  });

  it("Dismiss → clears the shared store (local dismiss, no backend call)", async () => {
    setPlaytimeScopeState({ pending: true });
    const { getByText } = render(<PlaytimeScopeBanner />);
    await act(async () => {
      fireEvent.click(getByText("Dismiss"));
      await flushAsync();
    });
    // Store flips to not-pending → the MainPage conditional drops the banner.
    // No backend dismiss callable exists — the click must not have invoked one.
    expect(getPlaytimeScopeState()).toEqual({ pending: false });
    expect(getPlaytimeScopeNotice).not.toHaveBeenCalled();
  });
});

describe("PlaytimeScopeBanner store-driven visibility", () => {
  beforeEach(() => {
    vi.mocked(getPlaytimeScopeNotice).mockReset();
    setPlaytimeScopeState({ pending: false });
  });

  it("shows the banner when the callable reports pending:true", async () => {
    vi.mocked(getPlaytimeScopeNotice).mockResolvedValue({ pending: true });
    const { queryByText } = render(<ScopeBannerHost />);
    await flushAsync();
    expect(queryByText(PLAYTIME_SCOPE_MESSAGE)).toBeInTheDocument();
  });

  it("keeps the banner absent when the callable reports pending:false", async () => {
    vi.mocked(getPlaytimeScopeNotice).mockResolvedValue({ pending: false });
    const { queryByText } = render(<ScopeBannerHost />);
    await flushAsync();
    expect(queryByText(PLAYTIME_SCOPE_MESSAGE)).not.toBeInTheDocument();
  });

  it("Dismiss hides a shown banner", async () => {
    vi.mocked(getPlaytimeScopeNotice).mockResolvedValue({ pending: true });
    const { getByText, queryByText } = render(<ScopeBannerHost />);
    await flushAsync();
    expect(queryByText(PLAYTIME_SCOPE_MESSAGE)).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(getByText("Dismiss"));
      await flushAsync();
    });
    expect(queryByText(PLAYTIME_SCOPE_MESSAGE)).not.toBeInTheDocument();
  });
});
