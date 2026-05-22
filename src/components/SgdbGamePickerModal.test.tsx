import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, act, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { toaster } from "@decky/api";
import * as backend from "../api/backend";
import * as artwork from "../utils/artwork";
import { SgdbGamePickerModalContent } from "./SgdbGamePickerModal";

// applyArtwork is mocked so the modal's apply path is observable without
// reaching into SteamClient / getSgdbArtworkBase64 plumbing.
vi.mock("../utils/artwork", () => ({
  applyArtwork: vi.fn(),
}));

// Find a <button> whose text content contains `text`.
function buttonContaining(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) =>
    (b.textContent ?? "").includes(text),
  );
  if (!btn) throw new Error(`button containing "${text}" not found`);
  return btn as HTMLButtonElement;
}

const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

describe("SgdbGamePickerModal", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(artwork.applyArtwork).mockResolvedValue(4);
    vi.mocked(backend.applySgdbGameId).mockResolvedValue({ success: true });
    vi.mocked(backend.searchSgdbGames).mockResolvedValue({ success: true, games: [] });
    vi.mocked(backend.debugLog).mockResolvedValue(undefined);
  });

  // ----- needs_pick / candidate entry point -----

  describe("candidates", () => {
    it("renders initial candidates and selecting one applies (2-arg)", async () => {
      const onApplied = vi.fn();
      const closeModal = vi.fn();
      const { container } = render(
        createElement(SgdbGamePickerModalContent, {
          romId: 88,
          appId: 6000,
          romName: "Mario",
          candidates: [
            { id: 1, name: "Super Mario", release_year: 1985, thumb_url: "https://x/m.png" },
          ],
          onApplied,
          closeModal,
        }),
      );
      expect(container.textContent).toContain("Super Mario");
      expect(container.textContent).toContain("1985");
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Super Mario"));
      });
      await flushAsync();
      expect(vi.mocked(backend.applySgdbGameId)).toHaveBeenCalledWith(88, 1);
      expect(vi.mocked(artwork.applyArtwork)).toHaveBeenCalledWith(88, 6000);
      expect(onApplied).toHaveBeenCalledWith(4);
      expect(closeModal).toHaveBeenCalledTimes(1);
    });

    it("shows the 'why shown' explanation under the rom name", () => {
      const { container } = render(
        createElement(SgdbGamePickerModalContent, {
          romId: 88,
          appId: 6000,
          romName: "Mario",
          candidates: [],
          onApplied: vi.fn(),
          closeModal: vi.fn(),
        }),
      );
      expect(container.textContent).toContain(
        "No SteamGridDB match was found automatically",
      );
    });

    it("does not render the 'top 6' note when there are no results", () => {
      const { container } = render(
        createElement(SgdbGamePickerModalContent, {
          romId: 88,
          appId: 6000,
          romName: "Mario",
          candidates: [],
          onApplied: vi.fn(),
          closeModal: vi.fn(),
        }),
      );
      expect(container.textContent).not.toContain("Showing the top 6 matches");
    });

    it("renders the 'top 6' note when results are present", () => {
      const { container } = render(
        createElement(SgdbGamePickerModalContent, {
          romId: 88,
          appId: 6000,
          romName: "Mario",
          candidates: [
            { id: 1, name: "Super Mario", release_year: 1985, thumb_url: null },
          ],
          onApplied: vi.fn(),
          closeModal: vi.fn(),
        }),
      );
      expect(container.textContent).toContain("Showing the top 6 matches");
    });

    it("renders a placeholder when a candidate thumb_url is null", () => {
      const { container } = render(
        createElement(SgdbGamePickerModalContent, {
          romId: 88,
          appId: 6000,
          romName: "Mario",
          candidates: [{ id: 1, name: "Super Mario", release_year: null, thumb_url: null }],
          onApplied: vi.fn(),
          closeModal: vi.fn(),
        }),
      );
      expect(container.textContent).toContain("No preview");
    });
  });

  // ----- search flow -----

  describe("search", () => {
    function renderPicker() {
      const onApplied = vi.fn();
      const closeModal = vi.fn();
      const ui = render(
        createElement(SgdbGamePickerModalContent, {
          romId: 99,
          appId: 7000,
          romName: "Zelda",
          onApplied,
          closeModal,
        }),
      );
      return { ...ui, onApplied, closeModal };
    }

    it("prefills the search field with the rom name", () => {
      const { container } = renderPicker();
      const input = container.querySelector('input[data-testid="text-field"]') as HTMLInputElement;
      expect(input.value).toBe("Zelda");
    });

    it("Search button calls searchSgdbGames and renders results", async () => {
      vi.mocked(backend.searchSgdbGames).mockResolvedValue({
        success: true,
        games: [{ id: 7, name: "Link's Awakening", release_year: 1993, thumb_url: null }],
      });
      const { container } = renderPicker();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Search"));
      });
      await flushAsync();
      expect(vi.mocked(backend.searchSgdbGames)).toHaveBeenCalledWith("Zelda");
      expect(container.textContent).toContain("Link's Awakening");
    });

    it("selecting a search result applies (2-arg)", async () => {
      vi.mocked(backend.searchSgdbGames).mockResolvedValue({
        success: true,
        games: [{ id: 7, name: "Link's Awakening", release_year: 1993, thumb_url: null }],
      });
      const { container } = renderPicker();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Search"));
      });
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Link's Awakening"));
      });
      await flushAsync();
      expect(vi.mocked(backend.applySgdbGameId)).toHaveBeenCalledWith(99, 7);
    });

    it("empty search results surface a 'No matches found.' message", async () => {
      vi.mocked(backend.searchSgdbGames).mockResolvedValue({ success: true, games: [] });
      const { container } = renderPicker();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Search"));
      });
      await flushAsync();
      expect(container.textContent).toContain("No matches found.");
    });

    it("search rejection surfaces an error message + debugLogs (non-vacuous catch)", async () => {
      vi.mocked(backend.searchSgdbGames).mockRejectedValue(new Error("net"));
      const { container } = renderPicker();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Search"));
      });
      await flushAsync();
      // Catch's observable effects: the inline error + the debugLog.
      expect(container.textContent).toContain("Search failed");
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
        expect.stringContaining("searchSgdbGames rejected"),
      );
    });

    it("R2 (TRIGGER_RIGHT) on the body fires a search", async () => {
      vi.mocked(backend.searchSgdbGames).mockResolvedValue({
        success: true,
        games: [{ id: 7, name: "Link's Awakening", release_year: 1993, thumb_url: null }],
      });
      const { container } = renderPicker();
      // The outer body Focusable is the first one in the tree; it carries the
      // onButtonDown handler (wired by the test mock to a "decky-button-down"
      // DOM event). GamepadButton.TRIGGER_RIGHT === 8.
      const body = container.querySelector('[data-testid="focusable"]') as HTMLElement;
      await act(async () => {
        fireEvent(
          body,
          new CustomEvent("decky-button-down", { detail: { button: 8 } }),
        );
      });
      await flushAsync();
      expect(vi.mocked(backend.searchSgdbGames)).toHaveBeenCalledWith("Zelda");
      expect(container.textContent).toContain("Link's Awakening");
    });

    it("a non-R2 button on the body does NOT fire a search", async () => {
      const { container } = renderPicker();
      const body = container.querySelector('[data-testid="focusable"]') as HTMLElement;
      await act(async () => {
        // GamepadButton.DIR_DOWN === 10 — navigation, not search.
        fireEvent(
          body,
          new CustomEvent("decky-button-down", { detail: { button: 10 } }),
        );
      });
      await flushAsync();
      expect(vi.mocked(backend.searchSgdbGames)).not.toHaveBeenCalled();
    });

    it("unsuccessful (success:false) search surfaces an error message", async () => {
      vi.mocked(backend.searchSgdbGames).mockResolvedValue({ success: false, games: [] });
      const { container } = renderPicker();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Search"));
      });
      await flushAsync();
      expect(container.textContent).toContain("Search failed");
    });
  });

  // ----- apply failure paths -----

  describe("apply failures", () => {
    function renderCandidate() {
      const onApplied = vi.fn();
      const closeModal = vi.fn();
      const ui = render(
        createElement(SgdbGamePickerModalContent, {
          romId: 88,
          appId: 6000,
          romName: "Mario",
          candidates: [{ id: 1, name: "Super Mario", release_year: null, thumb_url: null }],
          onApplied,
          closeModal,
        }),
      );
      return { ...ui, onApplied, closeModal };
    }

    it("applySgdbGameId failure → 'Failed to apply artwork selection', no applyArtwork, no close", async () => {
      vi.mocked(backend.applySgdbGameId).mockResolvedValue({ success: false });
      const { container, onApplied, closeModal } = renderCandidate();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Super Mario"));
      });
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Failed to apply artwork selection" }),
      );
      expect(vi.mocked(artwork.applyArtwork)).not.toHaveBeenCalled();
      expect(onApplied).not.toHaveBeenCalled();
      expect(closeModal).not.toHaveBeenCalled();
    });

    it("applySgdbGameId rejection → 'Failed to apply artwork selection' + debugLogs (non-vacuous catch)", async () => {
      vi.mocked(backend.applySgdbGameId).mockRejectedValue(new Error("net"));
      const { container } = renderCandidate();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Super Mario"));
      });
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Failed to apply artwork selection" }),
      );
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
        expect.stringContaining("applySgdbGameId rejected"),
      );
    });

    it("applyArtwork returning -1 → key toast", async () => {
      vi.mocked(artwork.applyArtwork).mockResolvedValue(-1);
      const { container } = renderCandidate();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Super Mario"));
      });
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Set a SteamGridDB API key in settings first" }),
      );
    });

    it("applyArtwork returning 0 → 'No artwork available for this game'", async () => {
      vi.mocked(artwork.applyArtwork).mockResolvedValue(0);
      const { container } = renderCandidate();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Super Mario"));
      });
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "No artwork available for this game" }),
      );
    });

    it("applyArtwork rejection → treated as 0 applied + debugLogs (non-vacuous catch)", async () => {
      vi.mocked(artwork.applyArtwork).mockRejectedValue(new Error("io"));
      const { container, onApplied } = renderCandidate();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Super Mario"));
      });
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "No artwork available for this game" }),
      );
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
        expect.stringContaining("applyArtwork rejected"),
      );
      expect(onApplied).toHaveBeenCalledWith(0);
    });
  });
});
