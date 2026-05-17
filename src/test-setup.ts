import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { createElement } from "react";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// Steam Deck ambient globals — minimal stubs; individual tests refine via vi.mocked.
vi.stubGlobal("SteamClient", {
  Apps: {
    AddShortcut: vi.fn(),
    SetShortcutName: vi.fn(),
    SetShortcutExe: vi.fn(),
    SetShortcutStartDir: vi.fn(),
    SetAppLaunchOptions: vi.fn(),
    RemoveShortcut: vi.fn(),
  },
  GameSessions: {
    RegisterForAppLifetimeNotifications: vi.fn(() => ({ unregister: vi.fn() })),
  },
  System: {
    GetSystemInfo: vi.fn().mockResolvedValue({ sHostname: "test" }),
    RegisterForOnSuspendRequest: vi.fn(() => ({ unregister: vi.fn() })),
    RegisterForOnResumeFromSuspend: vi.fn(() => ({ unregister: vi.fn() })),
  },
});
vi.stubGlobal("appStore", { GetAppOverviewByAppID: vi.fn(), allApps: [] });
vi.stubGlobal("appDetailsStore", { GetAppDetails: vi.fn() });
vi.stubGlobal("appDetailsCache", { GetAppData: vi.fn() });
vi.stubGlobal("collectionStore", { userCollections: [] });

// @decky/api — callable returns a vi.fn that resolves to undefined by default.
// Tests opt into specific behavior via vi.mocked(<callable>).mockResolvedValue(...).
vi.mock("@decky/api", () => ({
  callable: <T,>(_name: string) => vi.fn().mockResolvedValue(undefined) as unknown as T,
  toaster: { toast: vi.fn() },
  definePlugin: (fn: unknown) => fn,
}));

// @decky/ui — explicit pass-through stubs. Auto-mock yields undefined components
// and breaks RTL render() with "Element type is invalid".
vi.mock("@decky/ui", () => {
  type AnyProps = Record<string, unknown> & { children?: unknown };
  const passthrough = (tag: string) => (props: AnyProps) =>
    createElement(tag, props, props.children as never);
  return {
    ConfirmModal: passthrough("div"),
    ModalRoot: passthrough("div"),
    DialogButton: ({ children, onClick }: AnyProps) =>
      createElement("button", { onClick }, children as never),
    DialogButtonPrimary: ({ children, onClick }: AnyProps) =>
      createElement("button", { onClick }, children as never),
    Focusable: passthrough("div"),
    PanelSection: passthrough("section"),
    PanelSectionRow: passthrough("div"),
    TextField: (p: AnyProps & { value?: string; onChange?: (e: unknown) => void }) =>
      createElement("input", {
        value: p.value ?? "",
        onChange: (e: unknown) => p.onChange?.(e),
      }),
    ToggleField: (p: AnyProps & { checked?: boolean; onChange?: (v: boolean) => void }) =>
      createElement("input", {
        type: "checkbox",
        checked: p.checked ?? false,
        onChange: (e: { target: { checked: boolean } }) => p.onChange?.(e.target.checked),
      }),
    Dropdown: passthrough("select"),
    showModal: vi.fn(),
    Router: { CloseSideMenus: vi.fn(), Navigate: vi.fn() },
  };
});
