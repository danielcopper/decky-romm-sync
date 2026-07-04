import { describe, it, expect, vi, beforeEach } from "vitest";
import { readRunningApps, readPrimaryRunningApp, isAppRunning } from "./runningApps";

// The util reads bare Steam SP globals (`Router`, `SteamUIStore`). Each test
// stubs only the surfaces it exercises; the global afterEach in test-setup.ts
// runs vi.unstubAllGlobals(), so an unstubbed global reads as truly absent
// (`typeof X === "undefined"`) rather than leaking across tests.

describe("runningApps — defensive multi-source detection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe("readRunningApps", () => {
    it("reads Router.MainRunningApp as the primary source", () => {
      vi.stubGlobal("Router", { MainRunningApp: { appid: 100, display_name: "Game" } });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([{ appid: 100, display_name: "Game" }]);
      expect(diagnostics).toContain("Router.MainRunningApp=appid=100");
    });

    it("reports null when Router.MainRunningApp is null and finds nothing", () => {
      vi.stubGlobal("Router", { MainRunningApp: null });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([]);
      expect(diagnostics).toContain("Router.MainRunningApp=null");
    });

    it("reports no-Router when the global is absent, without throwing", () => {
      // Router intentionally not stubbed — a bare `=== undefined` would throw
      // ReferenceError; the util's typeof guard degrades to a note instead.
      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([]);
      expect(diagnostics).toContain("Router.MainRunningApp=no-Router");
      expect(diagnostics).toContain("SteamUIStore.RunningApps=no-store");
    });

    it("reads Router.RunningApps as a list source", () => {
      vi.stubGlobal("Router", {
        MainRunningApp: null,
        RunningApps: [
          { appid: 5, display_name: "A" },
          { appid: 6, strDisplayName: "B" },
        ],
      });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([
        { appid: 5, display_name: "A" },
        { appid: 6, display_name: "B" },
      ]);
      expect(diagnostics).toContain("Router.RunningApps=[5,6]");
    });

    it("reads SteamUIStore.RunningApps as a list source", () => {
      vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: 42, display_name: "Zelda" }] });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([{ appid: 42, display_name: "Zelda" }]);
      expect(diagnostics).toContain("SteamUIStore.RunningApps=[42]");
    });

    it("merges + de-dupes across sources, MainRunningApp first", () => {
      vi.stubGlobal("Router", {
        MainRunningApp: { appid: 100, display_name: "Main" },
        RunningApps: [{ appid: 100, display_name: "dup" }],
      });
      vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: 200, display_name: "Second" }] });

      const { apps } = readRunningApps();
      // 100 de-duped (MainRunningApp wins first-writer), 200 appended.
      expect(apps).toEqual([
        { appid: 100, display_name: "Main" },
        { appid: 200, display_name: "Second" },
      ]);
    });

    it("tolerates a throwing MainRunningApp getter and still reads other sources", () => {
      vi.stubGlobal("Router", {
        get MainRunningApp() {
          throw new Error("bridge fault");
        },
        RunningApps: [{ appid: 7, display_name: "Survivor" }],
      });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([{ appid: 7, display_name: "Survivor" }]);
      expect(diagnostics).toContain("Router.MainRunningApp=threw:");
    });

    it("drops entries without a numeric appid and reports an empty list", () => {
      vi.stubGlobal("Router", {
        MainRunningApp: null,
        RunningApps: [{ display_name: "no id" }, 42, null],
      });

      const { apps, diagnostics } = readRunningApps();
      expect(apps).toEqual([]);
      expect(diagnostics).toContain("Router.RunningApps=empty");
    });

    it("coerces a non-array iterable (MobX-style observable) source", () => {
      const observable = {
        *[Symbol.iterator]() {
          yield { appid: 11, display_name: "Obs" };
        },
      };
      vi.stubGlobal("SteamUIStore", { RunningApps: observable });

      const { apps } = readRunningApps();
      expect(apps).toEqual([{ appid: 11, display_name: "Obs" }]);
    });

    it("reports absent for a missing list property", () => {
      vi.stubGlobal("Router", { MainRunningApp: null });

      const { diagnostics } = readRunningApps();
      expect(diagnostics).toContain("Router.RunningApps=absent");
    });
  });

  describe("readPrimaryRunningApp", () => {
    it("returns the first running app and the round diagnostics", () => {
      vi.stubGlobal("Router", { MainRunningApp: { appid: 100, display_name: "Game" } });

      const { app, diagnostics } = readPrimaryRunningApp();
      expect(app).toEqual({ appid: 100, display_name: "Game" });
      expect(diagnostics).toContain("Router.MainRunningApp=appid=100");
    });

    it("returns null when no source reports a running app", () => {
      vi.stubGlobal("Router", { MainRunningApp: null });

      expect(readPrimaryRunningApp().app).toBeNull();
    });
  });

  describe("isAppRunning", () => {
    it("is true when the appId is reported by any source", () => {
      vi.stubGlobal("Router", { MainRunningApp: null });
      vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: 100, display_name: "Game" }] });

      expect(isAppRunning(100)).toBe(true);
    });

    it("is false when no source reports the appId", () => {
      vi.stubGlobal("Router", { MainRunningApp: { appid: 999, display_name: "Other" } });

      expect(isAppRunning(100)).toBe(false);
    });

    it("is false and does not throw when every source is absent", () => {
      expect(isAppRunning(100)).toBe(false);
    });
  });
});
