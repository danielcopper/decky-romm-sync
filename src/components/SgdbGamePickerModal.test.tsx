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

  // ----- conflict entry point -----

  describe("conflict tiles", () => {
    function renderConflict(overrides: Record<string, unknown> = {}) {
      const onApplied = vi.fn();
      const closeModal = vi.fn();
      const ui = render(
        createElement(SgdbGamePickerModalContent, {
          romId: 77,
          appId: 5000,
          romName: "Sonic",
          stateTile: { id: 11, thumb_url: "https://x/state.png" },
          rommTile: { id: 22, thumb_url: "https://x/romm.png" },
          onApplied,
          closeModal,
          ...overrides,
        }),
      );
      return { ...ui, onApplied, closeModal };
    }

    it("renders both tiles with 'Current' and 'From RomM' labels", () => {
      const { container } = renderConflict();
      expect(container.textContent).toContain("Current");
      expect(container.textContent).toContain("From RomM");
      // Both thumbnails render as <img>.
      const imgs = container.querySelectorAll("img");
      const srcs = Array.from(imgs).map((i) => i.getAttribute("src"));
      expect(srcs).toContain("https://x/state.png");
      expect(srcs).toContain("https://x/romm.png");
    });

    it("selecting the RomM tile applies with source 'romm', triggers applyArtwork, onApplied + close", async () => {
      const { container, onApplied, closeModal } = renderConflict();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "From RomM"));
      });
      await flushAsync();
      expect(vi.mocked(backend.applySgdbGameId)).toHaveBeenCalledWith(77, 22, "romm");
      expect(vi.mocked(artwork.applyArtwork)).toHaveBeenCalledWith(77, 5000);
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Artwork refreshed (4/4 images applied)" }),
      );
      expect(onApplied).toHaveBeenCalledWith(4);
      expect(closeModal).toHaveBeenCalledTimes(1);
    });

    it("selecting the Current tile applies with source 'keep' (preserves provenance)", async () => {
      const { container } = renderConflict();
      await act(async () => {
        fireEvent.click(buttonContaining(container, "Current"));
      });
      await flushAsync();
      expect(vi.mocked(backend.applySgdbGameId)).toHaveBeenCalledWith(77, 11, "keep");
    });

    it("renders a placeholder when a tile thumb_url is null", () => {
      const { container } = renderConflict({
        stateTile: { id: 11, thumb_url: null },
      });
      expect(container.textContent).toContain("No preview");
    });
  });

  // ----- needs_pick / candidate entry point -----

  describe("candidates", () => {
    it("renders initial candidates and selecting one applies with source 'manual'", async () => {
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
      expect(vi.mocked(backend.applySgdbGameId)).toHaveBeenCalledWith(88, 1, "manual");
      expect(vi.mocked(artwork.applyArtwork)).toHaveBeenCalledWith(88, 6000);
      expect(onApplied).toHaveBeenCalledWith(4);
      expect(closeModal).toHaveBeenCalledTimes(1);
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

    it("selecting a search result applies with source 'manual'", async () => {
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
      expect(vi.mocked(backend.applySgdbGameId)).toHaveBeenCalledWith(99, 7, "manual");
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
