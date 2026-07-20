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
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import type { FirmwarePlatformExt } from "../types";

// scrollToTop is a no-op in happy-dom; mock for cleanliness.
vi.mock("../utils/scrollHelpers", () => ({ scrollToTop: vi.fn() }));

// setLaunchOptionsConfirmed pokes SteamClient — stub it so the per-platform
// core-change fan-out (re-bake of each bound shortcut's launch_options) is
// driveable without a real Steam client.
vi.mock("../utils/steamShortcuts", () => ({ setLaunchOptionsConfirmed: vi.fn() }));

// SystemPage drives most of its tree through the global @decky/ui stub, but the
// per-platform emulator picker (#1210) is a ButtonItem that opens a context menu
// via buildEmulatorMenu — mocked separately below. Re-mock @decky/ui locally so
// ButtonItem renders a real <button> the tests can click, and showContextMenu is
// a sink; every other component mirrors the global stub.
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
    Spinner: () => ce("div", { "data-testid": "spinner" }),
    // ConfirmModal is passed to showModal as a created element; the test reads
    // its props (strTitle / onOK) off the captured showModal call rather than
    // rendering it, mirroring DangerZone.test.tsx.
    ConfirmModal: passthrough("div"),
    showModal: vi.fn(),
    // The per-platform emulator picker opens a context menu; the menu element is
    // built by the mocked buildEmulatorMenu below, so this is just a sink.
    showContextMenu: vi.fn(),
  };
});

// The per-platform emulator picker (#1210) opens the shared menu builder. Mock
// it to capture the config SystemPage hands it, so a test can drive its
// ``onPick(label)`` — the direct replacement for the old dropdown ``onChange``.
import type { EmulatorMenuConfig } from "../utils/emulatorMenu";
const capturedMenuConfigs: EmulatorMenuConfig[] = [];
vi.mock("../utils/emulatorMenu", () => ({
  buildEmulatorMenu: vi.fn((config: EmulatorMenuConfig) => {
    capturedMenuConfigs.push(config);
    return { __menu: true };
  }),
}));

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

// Find the "Emulator Core: …" ButtonItem (the #1210 picker trigger) and click
// it so SystemPage builds the menu config (captured via the mocked
// buildEmulatorMenu). Returns the captured config.
function openCoreMenu(container: HTMLElement): EmulatorMenuConfig {
  const btn = [...container.querySelectorAll("button")].find((b) => b.textContent.startsWith("Emulator Core:"));
  if (!btn) throw new Error("Emulator Core button not found");
  fireEvent.click(btn);
  const config = capturedMenuConfigs[capturedMenuConfigs.length - 1];
  if (!config) throw new Error("buildEmulatorMenu was not called");
  return config;
}

// Open the picker and drive its onPick(label) — the direct replacement for the
// old dropdown onChange — then flush the fire-and-forget handler.
const pickCore = (container: HTMLElement, label: string) =>
  act(async () => {
    openCoreMenu(container).onPick(label);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

function makeBiosPlatform(overrides: Partial<FirmwarePlatformExt> = {}): FirmwarePlatformExt {
  const files = overrides.files ?? [];
  // Mirror the backend per-platform BIOS aggregates (_set_platform_bios_aggregates):
  // the real get_firmware_status payload always ships these, so the helper derives
  // them from the files unless a test overrides them explicitly.
  const serverCount = files.length;
  const localCount = files.filter((f) => f.downloaded).length;
  const requiredFiles = files.filter((f) => f.classification === "required");
  const requiredCount = requiredFiles.length;
  const requiredDownloaded = requiredFiles.filter((f) => f.downloaded).length;
  const biosLevel: "ok" | "partial" | "missing" =
    requiredDownloaded >= requiredCount ? "ok" : requiredDownloaded > 0 ? "partial" : "missing";
  return {
    platform_slug: "snes",
    files: [],
    has_games: true,
    server_count: serverCount,
    local_count: localCount,
    required_count: requiredCount,
    required_downloaded: requiredDownloaded,
    bios_level: biosLevel,
    ...overrides,
  };
}

describe("SystemPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    capturedMenuConfigs.length = 0;
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
    vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(true);
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

    it("renders the no-synced-systems empty state when platforms list is empty", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("No synced systems");
    });

    it("renders only currently-synced systems (has_games), hiding unsynced ones", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({ platform_slug: "snes", has_games: true }),
          makeBiosPlatform({ platform_slug: "ps2", has_games: false }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      // The synced platform renders; the unsynced one is filtered out entirely.
      expect(container.textContent).toContain("snes");
      expect(container.textContent).not.toContain("ps2");
    });

    it("renders the no-synced-systems empty state when BIOS platforms exist but none are synced", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({ platform_slug: "snes", has_games: false }),
          makeBiosPlatform({ platform_slug: "ps2", has_games: false }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("No synced systems");
      // Neither unsynced platform is rendered as a section.
      expect(container.textContent).not.toContain("snes");
      expect(container.textContent).not.toContain("ps2");
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

    it("renders 'Not managed by the plugin' + a neutral grey dot for an unmanaged platform (#1520)", async () => {
      // Server files present but none registry-known → backend ships bios_level
      // "unmanaged". The System page must render honest neutral text (not a false
      // all-clear) with the shared grey status dot, and never flag it "BIOS needed".
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "psvita",
            bios_level: "unmanaged",
            files: [
              {
                id: 1,
                file_name: "unknown.bin",
                size: 100,
                md5: "x",
                downloaded: false,
                required: false,
                description: "?",
                hash_valid: null,
                classification: "unknown",
              },
            ],
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Not managed by the plugin");
      expect(container.textContent).toContain("1 file(s) on server the plugin doesn't recognise");
      // Neutral grey dot via the shared helper — never green.
      expect(container.innerHTML).toContain("#8f98a0");
      expect(container.innerHTML).not.toContain("#5ba32b");
      // Not flagged as needing BIOS (title carries no "BIOS needed" suffix).
      expect(container.textContent).not.toContain("BIOS needed");
    });
  });

  // ------------------------------------------------------------------
  // N. setSystemCore (core dropdown)
  // ------------------------------------------------------------------
  describe("setSystemCore", () => {
    it("does NOT render the core button when there is <=1 emulator", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            emulator_data_available: true,
            emulators: [
              {
                label: "snes9x",
                kind: "libretro",
                core_so: "snes9x.so",
                is_default: true,
                bakeable: true,
                reason: null,
              },
            ],
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      const coreBtn = [...container.querySelectorAll("button")].find((b) => b.textContent.startsWith("Emulator Core:"));
      expect(coreBtn).toBeUndefined();
    });

    it("renders the core button (with the active label) when there is >1 emulator", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            emulator_data_available: true,
            emulators: [
              {
                label: "snes9x",
                kind: "libretro",
                core_so: "snes9x.so",
                is_default: true,
                bakeable: true,
                reason: null,
              },
              {
                label: "mesen-s",
                kind: "libretro",
                core_so: "mesen-s.so",
                is_default: false,
                bakeable: true,
                reason: null,
              },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      const config = openCoreMenu(container);
      // The picker hands the classified emulator list + the active label + no
      // per-platform "(system)" marker (this page IS the system level).
      expect(config.emulators.map((e) => e.label)).toEqual(["snes9x", "mesen-s"]);
      expect(config.activeLabel).toBe("snes9x");
      expect(config.emulatorDataAvailable).toBe(true);
      expect(config.platformCoreLabel).toBeNull();
    });

    it("calls setSystemCore with empty label when default core is selected and dispatches romm_data_changed", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            emulator_data_available: true,
            emulators: [
              {
                label: "snes9x",
                kind: "libretro",
                core_so: "snes9x.so",
                is_default: true,
                bakeable: true,
                reason: null,
              },
              {
                label: "mesen-s",
                kind: "libretro",
                core_so: "mesen-s.so",
                is_default: false,
                bakeable: true,
                reason: null,
              },
            ],
            active_core_label: "mesen-s",
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await pickCore(container, "snes9x");
        // Picking the default core → label is "" sent to setSystemCore.
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
            emulator_data_available: true,
            emulators: [
              {
                label: "snes9x",
                kind: "libretro",
                core_so: "snes9x.so",
                is_default: true,
                bakeable: true,
                reason: null,
              },
              {
                label: "mesen-s",
                kind: "libretro",
                core_so: "mesen-s.so",
                is_default: false,
                bakeable: true,
                reason: null,
              },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await pickCore(container, "mesen-s");
      expect(vi.mocked(backend.setSystemCore)).toHaveBeenCalledWith("snes", "mesen-s");
    });

    it("reflects the applied per-platform emulator in the button label after picking and after remount (#1305)", async () => {
      const dualCore = (activeLabel: string) => ({
        success: true as const,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            emulator_data_available: true,
            emulators: [
              {
                label: "snes9x",
                kind: "libretro",
                core_so: "snes9x.so",
                is_default: true,
                bakeable: true,
                reason: null,
              },
              {
                label: "mesen-s",
                kind: "libretro",
                core_so: "mesen-s.so",
                is_default: false,
                bakeable: true,
                reason: null,
              },
            ],
            active_core_label: activeLabel,
          }),
        ],
      });
      // Mount shows the default; the post-pick refresh resolves the new selection
      // (the backend now returns it — the fix that makes get_firmware_status
      // reflect the per-platform override).
      vi.mocked(backend.getFirmwareStatus).mockResolvedValueOnce(dualCore("snes9x"));
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue(dualCore("mesen-s"));

      const { container, unmount } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      const coreBtn = () =>
        [...container.querySelectorAll("button")].find((b) => b.textContent.startsWith("Emulator Core:"));
      expect(coreBtn()?.textContent).toBe("Emulator Core: snes9x");

      await pickCore(container, "mesen-s");
      await flushAsync();
      // The System-page control reflects the just-applied selection immediately.
      expect(coreBtn()?.textContent).toBe("Emulator Core: mesen-s");

      // Remount → the persisted per-platform selection is still reflected.
      unmount();
      const { container: remounted } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      const remountedBtn = [...remounted.querySelectorAll("button")].find((b) =>
        b.textContent.startsWith("Emulator Core:"),
      );
      expect(remountedBtn?.textContent).toBe("Emulator Core: mesen-s");
    });

    // ----------------------------------------------------------------
    // Re-bake fan-out (#947): a successful per-platform core change
    // confirm-sets fresh launch_options for every affected bound shortcut
    // returned in result.rebake_items. Mirrors the migration_relaunch_options
    // fan-out in index.tsx (bounded-concurrency batches, per-item catch).
    // ----------------------------------------------------------------
    const renderDualCoreSnes = async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            emulator_data_available: true,
            emulators: [
              {
                label: "snes9x",
                kind: "libretro",
                core_so: "snes9x.so",
                is_default: true,
                bakeable: true,
                reason: null,
              },
              {
                label: "mesen-s",
                kind: "libretro",
                core_so: "mesen-s.so",
                is_default: false,
                bakeable: true,
                reason: null,
              },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      return container;
    };

    const selectMesenS = (container: HTMLElement) => pickCore(container, "mesen-s");

    it("confirm-sets launch options for each rebake item after a core change", async () => {
      const container = await renderDualCoreSnes();
      vi.mocked(backend.setSystemCore).mockResolvedValue({
        success: true,
        rebake_items: [
          { app_id: 100, launch_options: 'flatpak run net.retrodeck.retrodeck -e mesen-s "/roms/snes/a.sfc"' },
          { app_id: 200, launch_options: 'flatpak run net.retrodeck.retrodeck -e mesen-s "/roms/snes/b.sfc"' },
        ],
      });

      await selectMesenS(container);

      expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(
        100,
        'flatpak run net.retrodeck.retrodeck -e mesen-s "/roms/snes/a.sfc"',
      );
      expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(
        200,
        'flatpak run net.retrodeck.retrodeck -e mesen-s "/roms/snes/b.sfc"',
      );
      expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledTimes(2);
    });

    it("does not call setLaunchOptionsConfirmed when rebake_items is empty/absent", async () => {
      const container = await renderDualCoreSnes();
      // success but no rebake_items key at all (uninstalled / unbound platform)
      vi.mocked(backend.setSystemCore).mockResolvedValue({ success: true });

      await selectMesenS(container);

      expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
      // Still refreshes + dispatches the event so the UI reflects the new core.
      expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(2);
    });

    it("logs an error and keeps processing remaining items when one confirm rejects", async () => {
      // logError is a plain wrapper (not a callable mock) — spy it directly to
      // observe the post-catch side effect.
      const logErrorSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      try {
        const container = await renderDualCoreSnes();
        vi.mocked(backend.setSystemCore).mockResolvedValue({
          success: true,
          rebake_items: [
            { app_id: 100, launch_options: 'flatpak run net.retrodeck.retrodeck -e mesen-s "/roms/snes/a.sfc"' },
            { app_id: 200, launch_options: 'flatpak run net.retrodeck.retrodeck -e mesen-s "/roms/snes/b.sfc"' },
          ],
        });
        vi.mocked(setLaunchOptionsConfirmed).mockImplementation(async (appId: number) => {
          if (appId === 100) throw new Error("set failed");
          return true;
        });

        await selectMesenS(container);

        // CATCH-REJECTION assert: the rejecting item's error is surfaced via logError.
        expect(logErrorSpy).toHaveBeenCalledWith(
          expect.stringContaining("setSystemCore: failed to set launch options for appId 100"),
        );
        // Continued processing: the second item still got its confirm-set, and
        // the refresh/dispatch path still ran after the catch.
        expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(
          200,
          'flatpak run net.retrodeck.retrodeck -e mesen-s "/roms/snes/b.sfc"',
        );
        expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(2);
      } finally {
        logErrorSpy.mockRestore();
      }
    });

    it("logs an error when a confirm resolves false (write not confirmed)", async () => {
      const logErrorSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      try {
        const container = await renderDualCoreSnes();
        vi.mocked(backend.setSystemCore).mockResolvedValue({
          success: true,
          rebake_items: [
            { app_id: 300, launch_options: 'flatpak run net.retrodeck.retrodeck -e mesen-s "/roms/snes/c.sfc"' },
          ],
        });
        vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(false);

        await selectMesenS(container);

        // CATCH-REJECTION assert: the unconfirmed write surfaces a distinct logError.
        expect(logErrorSpy).toHaveBeenCalledWith(
          expect.stringContaining("setSystemCore: failed to confirm launch options for appId 300"),
        );
      } finally {
        logErrorSpy.mockRestore();
      }
    });

    it("does NOT fan out when setSystemCore returns success=false even if rebake_items present", async () => {
      const container = await renderDualCoreSnes();
      vi.mocked(backend.setSystemCore).mockResolvedValue({
        success: false,
        rebake_items: [{ app_id: 999, launch_options: "should-not-apply" }],
      });

      await selectMesenS(container);

      expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
    });

    it("does NOT refresh or dispatch the event when setSystemCore returns success=false", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            emulator_data_available: true,
            emulators: [
              {
                label: "snes9x",
                kind: "libretro",
                core_so: "snes9x.so",
                is_default: true,
                bakeable: true,
                reason: null,
              },
              {
                label: "mesen-s",
                kind: "libretro",
                core_so: "mesen-s.so",
                is_default: false,
                bakeable: true,
                reason: null,
              },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      vi.mocked(backend.setSystemCore).mockResolvedValue({ success: false });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await pickCore(container, "mesen-s");
        // refreshSystem was called once on mount; not again on failure
        expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(1);
        expect(listener).not.toHaveBeenCalled();
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("shows the fallback button label + null active in the picker when active_core_label is absent", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            emulator_data_available: true,
            emulators: [
              {
                label: "snes9x",
                kind: "libretro",
                core_so: "snes9x.so",
                is_default: true,
                bakeable: true,
                reason: null,
              },
              {
                label: "mesen-s",
                kind: "libretro",
                core_so: "mesen-s.so",
                is_default: false,
                bakeable: true,
                reason: null,
              },
            ],
            // No active_core_label
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      const coreBtn = [...container.querySelectorAll("button")].find((b) => b.textContent.startsWith("Emulator Core:"));
      // Button falls back to a "Default" hint; the picker passes a null active
      // label so the shared menu marks the default emulator with the checkmark.
      expect(coreBtn?.textContent).toBe("Emulator Core: Default");
      expect(openCoreMenu(container).activeLabel).toBeNull();
    });

    it("logs via debugLog when setSystemCore throws", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            emulator_data_available: true,
            emulators: [
              {
                label: "snes9x",
                kind: "libretro",
                core_so: "snes9x.so",
                is_default: true,
                bakeable: true,
                reason: null,
              },
              {
                label: "mesen-s",
                kind: "libretro",
                core_so: "mesen-s.so",
                is_default: false,
                bakeable: true,
                reason: null,
              },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      vi.mocked(backend.setSystemCore).mockRejectedValue(new Error("boom"));
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      await pickCore(container, "mesen-s");
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
            emulator_data_available: true,
            emulators: [
              {
                label: "snes9x",
                kind: "libretro",
                core_so: "snes9x.so",
                is_default: true,
                bakeable: true,
                reason: null,
              },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      // No core picker button rendered, but the "Emulator Core" Field is.
      const coreBtn = [...container.querySelectorAll("button")].find((b) => b.textContent.startsWith("Emulator Core:"));
      expect(coreBtn).toBeUndefined();
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

    it("dispatches a romm_data_changed {type:'bios', platform_slug} event on confirm + success", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithDownloaded()],
      });
      vi.mocked(backend.deletePlatformBios).mockResolvedValue({
        success: true,
        deleted_count: 1,
        message: "Deleted 1 BIOS file",
      });
      const dispatchSpy = vi.spyOn(globalThis, "dispatchEvent");
      const { getByText } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Delete BIOS (1)"));
      await act(async () => {
        lastConfirmModalProps()?.onOK?.();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      const biosEvents = dispatchSpy.mock.calls
        .map((c) => c[0])
        .filter((e): e is CustomEvent => e instanceof CustomEvent && e.type === "romm_data_changed");
      expect(biosEvents).toHaveLength(1);
      expect(biosEvents[0]!.detail).toEqual({ type: "bios", platform_slug: "ps1" });
      dispatchSpy.mockRestore();
    });

    it("does NOT dispatch romm_data_changed when deletePlatformBios reports success=false", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithDownloaded()],
      });
      vi.mocked(backend.deletePlatformBios).mockResolvedValue({
        success: false,
        deleted_count: 0,
        message: "Nothing to delete",
      });
      const dispatchSpy = vi.spyOn(globalThis, "dispatchEvent");
      const { getByText } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Delete BIOS (1)"));
      await act(async () => {
        lastConfirmModalProps()?.onOK?.();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      const biosEvents = dispatchSpy.mock.calls
        .map((c) => c[0])
        .filter((e): e is CustomEvent => e instanceof CustomEvent && e.type === "romm_data_changed");
      expect(biosEvents).toHaveLength(0);
      dispatchSpy.mockRestore();
    });
  });

  // ------------------------------------------------------------------
  // N3. Save-compatibility banner (#938) — shown once at the top, not per core
  // ------------------------------------------------------------------
  describe("save-compatibility banner", () => {
    function platformWithMultipleCores(slug: string): FirmwarePlatformExt {
      return makeBiosPlatform({
        platform_slug: slug,
        files: [],
        emulator_data_available: true,
        emulators: [
          { label: "core-a", kind: "libretro", core_so: "a.so", is_default: true, bakeable: true, reason: null },
          { label: "core-b", kind: "libretro", core_so: "b.so", is_default: false, bakeable: true, reason: null },
        ],
        active_core_label: "core-a",
      });
    }

    const BANNER = "Switching cores may affect save compatibility";

    function countOccurrences(haystack: string, needle: string): number {
      let count = 0;
      let idx = haystack.indexOf(needle);
      while (idx !== -1) {
        count++;
        idx = haystack.indexOf(needle, idx + needle.length);
      }
      return count;
    }

    it("renders the banner exactly once even with multiple multi-core platforms", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          platformWithMultipleCores("snes"),
          platformWithMultipleCores("ps1"),
          platformWithMultipleCores("n64"),
        ],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      // Three multi-core picker buttons render, but the banner is page-level (#938).
      const coreButtons = [...container.querySelectorAll("button")].filter((b) =>
        b.textContent.startsWith("Emulator Core:"),
      );
      expect(coreButtons.length).toBe(3);
      expect(countOccurrences(container.textContent, BANNER)).toBe(1);
    });

    it("renders the banner once even when there are no platforms at all", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [],
      });
      const { container } = render(<SystemPage onBack={vi.fn()} />);
      await flushAsync();
      expect(countOccurrences(container.textContent, BANNER)).toBe(1);
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
