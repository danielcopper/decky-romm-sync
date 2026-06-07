// CATCH-REJECTION ASSERTION RULE (applies to all orchestration shell tests):
// Every catch block with a setX(...) side effect MUST have its side effect
// asserted in the test (surfaced biosError / biosStatus string, debugLog spy,
// etc.). Asserting only that the rejecting call was invoked is vacuous — the
// rejection happens after the call returns so the test would pass with or
// without the .catch.
//
// SystemPage catch sites (all asserted below):
//   - refreshSystem try/catch → setBiosError(`Failed to fetch firmware status: ${e}`)
//   - refreshSystem failure branch → setBiosError(result.message || fallback)
//   - handleDownloadAll catch → setBiosStatus(`Download failed: ${e}`)
//   - handleDownloadRequired catch → setBiosStatus(`Download failed: ${e}`)
//   - handleDeleteBios catch → setBiosStatus(`Failed to delete BIOS files: ${e}`)
//   - setSystemCore onChange catch → debugLog(`setSystemCore: error: ${e}`)

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { showModal } from "@decky/ui";
import type { ReactElement } from "react";
import { SystemPage } from "./SystemPage";
import * as backend from "../api/backend";
import type { FirmwarePlatformExt } from "../types";

// scrollToTop is a no-op in happy-dom; mock for cleanliness.
vi.mock("../utils/scrollHelpers", () => ({ scrollToTop: vi.fn() }));

// DropdownItem in the global @decky/ui stub is a passthrough <select> with no
// rgOptions / onChange capture. SystemPage uses DropdownItem for per-platform
// core selection; we need to drive its onChange callback to test the
// setSystemCore flow. Re-mock @decky/ui locally to expose DropdownItem props on
// a shared captured-array — every other component mirrors the global stub so the
// rest of the tree behaves identically.
interface CapturedDropdown {
  label?: string;
  rgOptions?: Array<{ data: unknown; label: string }>;
  selectedOption?: unknown;
  onChange?: (option: { data: string; label: string }) => void | Promise<void>;
}
const capturedDropdowns: CapturedDropdown[] = [];

vi.mock("@decky/ui", async () => {
  const { createElement: ce } = await import("react");
  type AnyProps = Record<string, unknown> & { children?: unknown };
  const passthrough = (tag: string) => (p: AnyProps) => ce(tag, {}, p.children as never);
  return {
    PanelSection: (p: AnyProps & { title?: unknown }) =>
      ce(
        "section",
        { "data-testid": "panel-section", "data-title": typeof p.title === "string" ? p.title : undefined },
        typeof p.title === "string" ? ce("h2", { "data-testid": "panel-title" }, p.title) : null,
        p.children as never,
      ),
    PanelSectionRow: passthrough("div"),
    ButtonItem: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
      ce("button", { onClick, disabled }, children as never),
    Field: (p: AnyProps & { label?: unknown; description?: unknown }) =>
      ce(
        "div",
        { "data-testid": "field" },
        ce("span", { "data-testid": "field-label" }, p.label as never),
        ce("span", { "data-testid": "field-desc" }, p.description as never),
      ),
    Focusable: passthrough("div"),
    DropdownItem: (p: CapturedDropdown) => {
      capturedDropdowns.push(p);
      return ce(
        "div",
        { "data-testid": "dropdown" },
        ce("span", { "data-testid": "dropdown-label" }, p.label as never),
      );
    },
    Spinner: () => ce("div", { "data-testid": "spinner" }),
    // ConfirmModal is passed to showModal as a created element; the test reads
    // its props (strTitle / onOK) off the captured showModal call rather than
    // rendering it, mirroring DangerZone.test.tsx.
    ConfirmModal: passthrough("div"),
    showModal: vi.fn(),
  };
});

// Props of the ConfirmModal element handed to the most recent showModal() call.
interface ConfirmModalProps {
  strTitle?: string;
  strDescription?: string;
  strOKButtonText?: string;
  strCancelButtonText?: string;
  onOK?: () => void;
}
function lastConfirmModalProps(): ConfirmModalProps | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<ConfirmModalProps> | undefined;
  return el?.props ?? null;
}

// Flush mount-time + chained promise resolutions.
const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

function makeBiosPlatform(overrides: Partial<FirmwarePlatformExt> = {}): FirmwarePlatformExt {
  return {
    platform_slug: "snes",
    files: [],
    has_games: true,
    ...overrides,
  };
}

describe("SystemPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    capturedDropdowns.length = 0;
    // Default callable behavior — tests override per case.
    vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
      success: true,
      platforms: [],
    });
    vi.mocked(backend.downloadAllFirmware).mockResolvedValue({ success: true });
    vi.mocked(backend.downloadRequiredFirmware).mockResolvedValue({
      success: true,
    });
    vi.mocked(backend.deletePlatformBios).mockResolvedValue({
      success: true,
      deleted_count: 0,
      message: "",
    });
    vi.mocked(backend.setSystemCore).mockResolvedValue({ success: true });
  });

  // ------------------------------------------------------------------
  // Initial render — loads on mount (no tab click; this page IS the view)
  // ------------------------------------------------------------------
  describe("initial render", () => {
    it("calls getFirmwareStatus once on mount", async () => {
      render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(1);
    });

    it("renders the loading state before getFirmwareStatus resolves and removes it after", async () => {
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      // Initial render — getFirmwareStatus not yet resolved
      expect(container.textContent).toContain("Loading firmware status...");
      await flushAsync();
      expect(container.textContent).not.toContain("Loading firmware status...");
    });
  });

  // ------------------------------------------------------------------
  // I. refreshSystem
  // ------------------------------------------------------------------
  describe("refreshSystem", () => {
    it("renders platforms and sets serverOffline on success", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        server_offline: false,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "snes.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: true,
                description: "BIOS",
                hash_valid: true,
                classification: "required",
              },
            ],
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("snes");
    });

    it("renders the server-offline banner when getFirmwareStatus reports server_offline", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        server_offline: true,
        platforms: [],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Server offline");
    });

    it("surfaces result.message when getFirmwareStatus returns success=false with a message", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: false,
        message: "Server is sad",
        platforms: [],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      // CATCH-REJECTION (failure branch): biosError = result.message
      expect(container.textContent).toContain("Server is sad");
    });

    it("falls back to 'Failed to fetch firmware status' when result.message is absent", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: false,
        platforms: [],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Failed to fetch firmware status");
    });

    it("sets biosError='Failed to fetch firmware status: <e>' when getFirmwareStatus throws", async () => {
      vi.mocked(backend.getFirmwareStatus).mockRejectedValue(new Error("network"));
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      // CATCH-REJECTION assert: rendered with the interpolated Error
      expect(container.textContent).toContain("Failed to fetch firmware status: Error: network");
    });

    it("renders the no-firmware empty state when platforms list is empty", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("No firmware files found");
    });
  });

  // ------------------------------------------------------------------
  // J. handleDownloadAll
  // ------------------------------------------------------------------
  describe("handleDownloadAll", () => {
    function biosPlatformWithMissingOptional(): FirmwarePlatformExt {
      return makeBiosPlatform({
        platform_slug: "snes",
        files: [
          {
            id: 1,
            file_name: "boot.rom",
            size: 100,
            md5: "x",
            downloaded: false,
            required: false,
            description: "Optional",
            hash_valid: null,
            classification: "optional",
          },
        ],
      });
    }

    it("calls downloadAllFirmware(slug) and then refreshes on success", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingOptional()],
      });
      vi.mocked(backend.downloadAllFirmware).mockResolvedValue({
        success: true,
        downloaded: 1,
      });
      const { getByText } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Download All"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.downloadAllFirmware)).toHaveBeenCalledWith("snes");
      // refreshSystem called once on mount + once after download
      expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(2);
    });

    it("surfaces result.message when the download succeeds", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingOptional()],
      });
      vi.mocked(backend.downloadAllFirmware).mockResolvedValue({
        success: true,
        message: "All good",
        downloaded: 1,
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Download All"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("All good");
    });

    it("surfaces 'Download failed' when result.success=false with no message", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingOptional()],
      });
      vi.mocked(backend.downloadAllFirmware).mockResolvedValue({
        success: false,
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Download All"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Download failed");
    });

    it("sets biosStatus='Download failed: <e>' when downloadAllFirmware throws", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingOptional()],
      });
      vi.mocked(backend.downloadAllFirmware).mockRejectedValue(new Error("io"));
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Download All"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION assert: status string rendered
      expect(container.textContent).toContain("Download failed: Error: io");
    });
  });

  // ------------------------------------------------------------------
  // K. handleDownloadRequired
  // ------------------------------------------------------------------
  describe("handleDownloadRequired", () => {
    function biosPlatformWithMissingRequired(): FirmwarePlatformExt {
      return makeBiosPlatform({
        platform_slug: "snes",
        files: [
          {
            id: 1,
            file_name: "bios.rom",
            size: 100,
            md5: "x",
            downloaded: false,
            required: true,
            description: "Required BIOS",
            hash_valid: null,
            classification: "required",
          },
        ],
      });
    }

    it("calls downloadRequiredFirmware(slug) and refreshes on success", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingRequired()],
      });
      vi.mocked(backend.downloadRequiredFirmware).mockResolvedValue({
        success: true,
        downloaded: 1,
      });
      const { getByText } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Download Required"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.downloadRequiredFirmware)).toHaveBeenCalledWith("snes");
      expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(2);
    });

    it("surfaces 'Download failed: <e>' when downloadRequiredFirmware throws", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingRequired()],
      });
      vi.mocked(backend.downloadRequiredFirmware).mockRejectedValue(new Error("io"));
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Download Required"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION assert: status string rendered
      expect(container.textContent).toContain("Download failed: Error: io");
    });

    it("surfaces 'Download failed' fallback when result.success=false with no message", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingRequired()],
      });
      vi.mocked(backend.downloadRequiredFirmware).mockResolvedValue({
        success: false,
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Download Required"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Download failed");
    });
  });

  // ------------------------------------------------------------------
  // L. expand/collapse + hashIndicator + unknown summary
  // ------------------------------------------------------------------
  describe("expand/collapse and file rendering", () => {
    it("expands files on Show Files click and collapses on the same button", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "ok.bin",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "OK File",
                hash_valid: true,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      // Initially collapsed — file name not rendered
      expect(container.textContent).not.toContain("OK File");
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("OK File");
      // Now collapse
      await act(async () => {
        fireEvent.click(getByText("Hide Files"));
        await Promise.resolve();
      });
      expect(container.textContent).not.toContain("OK File");
    });

    it("renders hashIndicator ' ✓' for downloaded files with hash_valid=true", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "good.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "Good",
                hash_valid: true,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("good.rom ✓");
    });

    it("renders hashIndicator ' ⚠' for downloaded files with hash_valid=false", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "bad.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "Bad",
                hash_valid: false,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("bad.rom ⚠");
    });

    it("renders hashIndicator ' —' for downloaded files with hash_valid=null", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "unk.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "Unk",
                hash_valid: null,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("unk.rom —");
    });

    it("renders a missing required file (red dot branch) when expanded", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "missing-req.rom",
                size: 100,
                md5: "x",
                downloaded: false,
                required: true,
                description: "ReqMissing",
                hash_valid: null,
                classification: "required",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      // Missing required → "Missing" suffix and red dot branch
      expect(container.textContent).toContain("missing-req.rom — Missing");
    });

    it("renders a missing optional file (gray dot branch) when expanded", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "missing-opt.rom",
                size: 100,
                md5: "x",
                downloaded: false,
                required: false,
                description: "OptMissing",
                hash_valid: null,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      // Missing optional → "Missing" suffix and gray dot branch
      expect(container.textContent).toContain("missing-opt.rom — Missing");
    });

    it("renders the unrecognized-file footer when unknown files are present", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "mystery.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "?",
                hash_valid: null,
                classification: "unknown",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("1 file(s) not recognized");
    });
  });

  // ------------------------------------------------------------------
  // M. getBiosSummary indirect coverage via rendering
  // ------------------------------------------------------------------
  describe("summary text", () => {
    it("shows 'X / Y required' + 'All required ready' when all required are done and no optional missing", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "req.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: true,
                description: "Req",
                hash_valid: true,
                classification: "required",
              },
            ],
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("1 / 1 required");
      expect(container.textContent).toContain("All required ready");
    });

    it("shows 'N optional missing' when all required are done but optional is missing", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "req.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: true,
                description: "Req",
                hash_valid: true,
                classification: "required",
              },
              {
                id: 2,
                file_name: "opt.rom",
                size: 100,
                md5: "x",
                downloaded: false,
                required: false,
                description: "Opt",
                hash_valid: null,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("All required ready (1 optional missing)");
    });

    it("shows 'N required missing — games may not launch' when required is incomplete", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "req.rom",
                size: 100,
                md5: "x",
                downloaded: false,
                required: true,
                description: "Req",
                hash_valid: null,
                classification: "required",
              },
            ],
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("1 required missing");
    });

    it("falls back to 'X / Y files' summary when there are no required files", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "opt.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "Opt",
                hash_valid: true,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("1 / 1 files");
      expect(container.textContent).toContain("All downloaded");
    });

    it("shows 'N missing' suffix when not all files are downloaded and no required files exist", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "opt.rom",
                size: 100,
                md5: "x",
                downloaded: false,
                required: false,
                description: "Opt",
                hash_valid: null,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("0 / 1 files");
      expect(container.textContent).toContain("1 missing");
    });
  });

  // ------------------------------------------------------------------
  // N. setSystemCore (core dropdown)
  // ------------------------------------------------------------------
  describe("setSystemCore", () => {
    it("does NOT render the dropdown when available_cores has <=1 entry", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [{ core_so: "snes9x.so", label: "snes9x", is_default: true }],
          }),
        ],
      });
      render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedDropdowns.length).toBe(0);
    });

    it("renders the core dropdown when available_cores has >1 entries", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [
              { core_so: "snes9x.so", label: "snes9x", is_default: true },
              { core_so: "mesen-s.so", label: "mesen-s", is_default: false },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedDropdowns.length).toBe(1);
      const dropdown = capturedDropdowns[0]!;
      expect(dropdown.label).toBe("Emulator Core");
      expect(dropdown.rgOptions?.map((o) => o.data)).toEqual(["snes9x", "mesen-s"]);
      expect(dropdown.rgOptions?.[0]?.label).toBe("snes9x (default)");
      expect(dropdown.selectedOption).toBe("snes9x");
    });

    it("calls setSystemCore with empty label when default core is selected and dispatches romm_data_changed", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [
              { core_so: "snes9x.so", label: "snes9x", is_default: true },
              { core_so: "mesen-s.so", label: "mesen-s", is_default: false },
            ],
            active_core_label: "mesen-s",
          }),
        ],
      });
      render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await capturedDropdowns[0]?.onChange?.({
            data: "snes9x",
            label: "snes9x (default)",
          });
        });
        // Selecting the default core → label is "" sent to setSystemCore
        expect(vi.mocked(backend.setSystemCore)).toHaveBeenCalledWith("snes", "");
        expect(listener).toHaveBeenCalledTimes(1);
        const ev = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(ev.detail).toEqual({
          type: "core_changed",
          platform_slug: "snes",
        });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("calls setSystemCore with the explicit non-default label", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [
              { core_so: "snes9x.so", label: "snes9x", is_default: true },
              { core_so: "mesen-s.so", label: "mesen-s", is_default: false },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        await capturedDropdowns[0]?.onChange?.({
          data: "mesen-s",
          label: "mesen-s",
        });
      });
      expect(vi.mocked(backend.setSystemCore)).toHaveBeenCalledWith("snes", "mesen-s");
    });

    it("does NOT refresh or dispatch the event when setSystemCore returns success=false", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [
              { core_so: "snes9x.so", label: "snes9x", is_default: true },
              { core_so: "mesen-s.so", label: "mesen-s", is_default: false },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      vi.mocked(backend.setSystemCore).mockResolvedValue({ success: false });
      render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await capturedDropdowns[0]?.onChange?.({
            data: "mesen-s",
            label: "mesen-s",
          });
        });
        // refreshSystem was called once on mount; not again on failure
        expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(1);
        expect(listener).not.toHaveBeenCalled();
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("falls back to default core label when active_core_label is absent", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [
              { core_so: "snes9x.so", label: "snes9x", is_default: true },
              { core_so: "mesen-s.so", label: "mesen-s", is_default: false },
            ],
            // No active_core_label
          }),
        ],
      });
      render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedDropdowns[0]?.selectedOption).toBe("snes9x");
    });

    it("logs via debugLog when setSystemCore throws", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [
              { core_so: "snes9x.so", label: "snes9x", is_default: true },
              { core_so: "mesen-s.so", label: "mesen-s", is_default: false },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      vi.mocked(backend.setSystemCore).mockRejectedValue(new Error("boom"));
      render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        await capturedDropdowns[0]?.onChange?.({
          data: "mesen-s",
          label: "mesen-s",
        });
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION assert: error logged via debugLog
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith("setSystemCore: error: Error: boom");
    });

    it("renders an inactive Emulator Core Field when active_core_label is set but only 1 available core exists", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [{ core_so: "snes9x.so", label: "snes9x", is_default: true }],
            active_core_label: "snes9x",
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      // No dropdown rendered, but the "Emulator Core" Field is.
      expect(capturedDropdowns.length).toBe(0);
      expect(container.textContent).toContain("snes9x");
    });
  });

  // ------------------------------------------------------------------
  // N2. Delete BIOS (#933) — per-platform destructive action
  // ------------------------------------------------------------------
  describe("handleDeleteBios", () => {
    function biosPlatformWithDownloaded(): FirmwarePlatformExt {
      return makeBiosPlatform({
        platform_slug: "ps1",
        files: [
          {
            id: 1,
            file_name: "scph5501.bin",
            size: 100,
            md5: "x",
            downloaded: true,
            required: true,
            description: "PS1 BIOS",
            hash_valid: true,
            classification: "required",
          },
        ],
      });
    }

    function biosPlatformNothingDownloaded(): FirmwarePlatformExt {
      return makeBiosPlatform({
        platform_slug: "ps1",
        files: [
          {
            id: 1,
            file_name: "scph5501.bin",
            size: 100,
            md5: "x",
            downloaded: false,
            required: true,
            description: "PS1 BIOS",
            hash_valid: null,
            classification: "required",
          },
        ],
      });
    }

    it("hides the Delete BIOS button when no files are downloaded", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformNothingDownloaded()],
      });
      const { queryByText } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(queryByText(/Delete BIOS/)).toBeNull();
    });

    it("shows the Delete BIOS button with the downloaded count when at least one file is downloaded", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithDownloaded()],
      });
      const { getByText } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(getByText("Delete BIOS (1)")).toBeTruthy();
    });

    it("opens a ConfirmModal (does NOT call deletePlatformBios) when the Delete BIOS button is clicked", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithDownloaded()],
      });
      const { getByText } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Delete BIOS (1)"));
      // Confirmation gates the destructive call — nothing deleted yet.
      expect(vi.mocked(backend.deletePlatformBios)).not.toHaveBeenCalled();
      const props = lastConfirmModalProps();
      expect(props?.strTitle).toBe("Delete BIOS files for ps1?");
      expect(props?.strOKButtonText).toBe("Delete BIOS Files");
      expect(props?.strCancelButtonText).toBe("Cancel");
    });

    it("calls deletePlatformBios(slug), surfaces the message, and refreshes on confirm + success", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithDownloaded()],
      });
      vi.mocked(backend.deletePlatformBios).mockResolvedValue({
        success: true,
        deleted_count: 1,
        message: "Deleted 1 BIOS file",
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Delete BIOS (1)"));
      await act(async () => {
        lastConfirmModalProps()?.onOK?.();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.deletePlatformBios)).toHaveBeenCalledWith("ps1");
      // refreshSystem: once on mount + once after a successful delete.
      expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(2);
      expect(container.textContent).toContain("Deleted 1 BIOS file");
    });

    it("surfaces the failure message and does NOT refresh when deletePlatformBios reports success=false", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithDownloaded()],
      });
      vi.mocked(backend.deletePlatformBios).mockResolvedValue({
        success: false,
        deleted_count: 0,
        message: "Nothing to delete",
      });
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Delete BIOS (1)"));
      await act(async () => {
        lastConfirmModalProps()?.onOK?.();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.deletePlatformBios)).toHaveBeenCalledWith("ps1");
      // Only the mount-time refresh — no second refresh on failure.
      expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(1);
      expect(container.textContent).toContain("Nothing to delete");
    });

    it("sets biosStatus='Failed to delete BIOS files: <e>' when deletePlatformBios throws", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithDownloaded()],
      });
      vi.mocked(backend.deletePlatformBios).mockRejectedValue(new Error("io"));
      const { getByText, container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Delete BIOS (1)"));
      await act(async () => {
        lastConfirmModalProps()?.onOK?.();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION assert: status string rendered.
      expect(container.textContent).toContain("Failed to delete BIOS files: Error: io");
    });
  });

  // ------------------------------------------------------------------
  // O. Back button
  // ------------------------------------------------------------------
  describe("back button", () => {
    it("invokes onBack when the Back button is clicked", async () => {
      const onBack = vi.fn();
      const { getByText } = render(<SystemPage onBack={onBack} />);
      await flushAsync();
      fireEvent.click(getByText("Back"));
      expect(onBack).toHaveBeenCalledTimes(1);
    });
  });
});
