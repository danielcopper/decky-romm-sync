import { describe, it, expect, vi, beforeEach } from "vitest";
import { readRunningApps, readPrimaryRunningApp, isAppRunning, isAnyAppRunning } from "./runningApps";

// The util reads the bare Steam SP global `SteamUIStore`. Each test stubs only
// what it exercises; the global afterEach in test-setup.ts runs
// vi.unstubAllGlobals(), so an unstubbed global reads as truly absent
// (`typeof X === "undefined"`) rather than leaking across tests.

describe("runningApps — guarded SteamUIStore reader", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe("readRunningApps", () => {
    it("reads the running apps the store reports", () => {
      vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: 42, display_name: "Zelda" }] });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([{ appid: 42, display_name: "Zelda" }]);
      expect(diagnostics).toBe("SteamUIStore.RunningApps=[42]");
    });

    it("preserves store order, so the foreground app stays first", () => {
      // Steam derives RunningApps from one m_runningAppIDs array whose head is
      // MainRunningApp — the reader must not reorder it.
      vi.stubGlobal("SteamUIStore", {
        RunningApps: [
          { appid: 100, display_name: "Foreground" },
          { appid: 200, display_name: "Background" },
        ],
      });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([
        { appid: 100, display_name: "Foreground" },
        { appid: 200, display_name: "Background" },
      ]);
      expect(diagnostics).toBe("SteamUIStore.RunningApps=[100,200]");
    });

    it("falls back to strDisplayName when display_name is absent", () => {
      vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: 6, strDisplayName: "B" }] });

      expect(readRunningApps().apps).toEqual([{ appid: 6, display_name: "B" }]);
    });

    it("keeps a nameless entry with an empty display_name rather than dropping it", () => {
      // The appid is what every consumer matches on; a missing name must not
      // make a genuinely running app invisible.
      vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: 8 }] });

      expect(readRunningApps().apps).toEqual([{ appid: 8, display_name: "" }]);
    });

    it("reports an empty list as empty — a running game may still be up (post-restart window)", () => {
      vi.stubGlobal("SteamUIStore", { RunningApps: [] });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([]);
      expect(diagnostics).toBe("SteamUIStore.RunningApps=empty");
    });

    it("reports absent when the store exposes no RunningApps property", () => {
      vi.stubGlobal("SteamUIStore", { SetRunningApp: vi.fn() });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([]);
      expect(diagnostics).toBe("SteamUIStore.RunningApps=absent");
    });

    it("reports no-store when the global is absent, without throwing", () => {
      // SteamUIStore intentionally not stubbed — a bare `=== undefined` would
      // throw ReferenceError; the util's typeof guard degrades to a note instead.
      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([]);
      expect(diagnostics).toBe("SteamUIStore.RunningApps=no-store");
    });

    it("reports no-store when the global is null", () => {
      vi.stubGlobal("SteamUIStore", null);

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([]);
      expect(diagnostics).toBe("SteamUIStore.RunningApps=no-store");
    });

    it("tolerates a throwing RunningApps getter and names it in the diagnostics", () => {
      vi.stubGlobal("SteamUIStore", {
        get RunningApps(): unknown {
          throw new Error("bridge fault");
        },
      });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([]);
      expect(diagnostics).toContain("SteamUIStore.RunningApps=threw:");
      expect(diagnostics).toContain("bridge fault");
    });

    it("drops entries without a numeric appid and reports an empty list", () => {
      vi.stubGlobal("SteamUIStore", { RunningApps: [{ display_name: "no id" }, 42, null] });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([]);
      expect(diagnostics).toBe("SteamUIStore.RunningApps=empty");
    });

    it("coerces a non-array iterable (MobX-style observable)", () => {
      const observable = {
        *[Symbol.iterator]() {
          yield { appid: 11, display_name: "Obs" };
        },
      };
      vi.stubGlobal("SteamUIStore", { RunningApps: observable });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([{ appid: 11, display_name: "Obs" }]);
      expect(diagnostics).toBe("SteamUIStore.RunningApps=[11]");
    });

    it("reports empty for a present but non-list value", () => {
      vi.stubGlobal("SteamUIStore", { RunningApps: 7 as unknown as SteamAppOverview[] });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([]);
      expect(diagnostics).toBe("SteamUIStore.RunningApps=empty");
    });
  });

  describe("readPrimaryRunningApp", () => {
    it("returns the head of the store's list — the foreground app — plus the diagnostics", () => {
      vi.stubGlobal("SteamUIStore", {
        RunningApps: [
          { appid: 100, display_name: "Foreground" },
          { appid: 200, display_name: "Background" },
        ],
      });

      const { app, diagnostics } = readPrimaryRunningApp();
      expect(app).toEqual({ appid: 100, display_name: "Foreground" });
      expect(diagnostics).toBe("SteamUIStore.RunningApps=[100,200]");
    });

    it("returns null when the store reports nothing running", () => {
      vi.stubGlobal("SteamUIStore", { RunningApps: [] });

      expect(readPrimaryRunningApp().app).toBeNull();
    });

    it("returns null and does not throw when the store is absent", () => {
      expect(readPrimaryRunningApp()).toEqual({ app: null, diagnostics: "SteamUIStore.RunningApps=no-store" });
    });
  });

  describe("isAppRunning", () => {
    it("is true when the appId is reported by the store", () => {
      vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: 100, display_name: "Game" }] });

      expect(isAppRunning(100)).toBe(true);
    });

    it("is true for a background entry, not just the foreground one", () => {
      vi.stubGlobal("SteamUIStore", {
        RunningApps: [
          { appid: 999, display_name: "Foreground" },
          { appid: 100, display_name: "Background" },
        ],
      });

      expect(isAppRunning(100)).toBe(true);
    });

    it("is false when the store reports a different appId", () => {
      vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: 999, display_name: "Other" }] });

      expect(isAppRunning(100)).toBe(false);
    });

    it("is false and does not throw when the store is absent", () => {
      expect(isAppRunning(100)).toBe(false);
    });
  });

  describe("isAnyAppRunning", () => {
    it("is true when the store reports any running app", () => {
      vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: 100, display_name: "Game" }] });

      expect(isAnyAppRunning()).toBe(true);
    });

    it("is false when the store reports an empty list", () => {
      vi.stubGlobal("SteamUIStore", { RunningApps: [] });

      expect(isAnyAppRunning()).toBe(false);
    });

    it("is false and does not throw when the store is absent", () => {
      expect(isAnyAppRunning()).toBe(false);
    });
  });
});
