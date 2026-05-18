import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { createElement } from "react";
import { resetDeckyEventBus } from "./test-utils/decky-api-mock";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  resetDeckyEventBus();
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
// addEventListener / removeEventListener route through the in-memory event bus
// in src/test-utils/decky-api-mock.ts so tests can drive Decky-loader events
// via emitDeckyEvent(). Async factory + dynamic import is required because
// vi.mock factories are hoisted above top-level imports.
vi.mock("@decky/api", async () => {
  const bus = await import("./test-utils/decky-api-mock");
  return {
    callable: <T,>(_name: string) => vi.fn().mockResolvedValue(undefined) as unknown as T,
    toaster: { toast: vi.fn() },
    definePlugin: (fn: unknown) => fn,
    addEventListener: bus.mockAddEventListener,
    removeEventListener: bus.mockRemoveEventListener,
  };
});

// @decky/ui — explicit pass-through stubs. Auto-mock yields undefined components
// and breaks RTL render() with "Element type is invalid".
//
// Component coverage targets the union of what frontend components actually
// render. Test files that need richer per-component behavior (e.g. capturing
// `rgOptions` off a DropdownItem) may locally re-mock `@decky/ui` — Vitest's
// per-file mock hoisting wins over this global stub.
vi.mock("@decky/ui", () => {
  type AnyProps = Record<string, unknown> & { children?: unknown };
  const passthrough = (tag: string) => (props: AnyProps) =>
    createElement(tag, props, props.children as never);
  return {
    ConfirmModal: passthrough("div"),
    ModalRoot: passthrough("div"),
    DialogButton: ({ children, onClick, disabled }: AnyProps & { disabled?: boolean }) =>
      createElement("button", { onClick, disabled }, children as never),
    DialogButtonPrimary: ({ children, onClick }: AnyProps) =>
      createElement("button", { onClick }, children as never),
    ButtonItem: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
      createElement("button", { onClick, disabled }, children as never),
    Field: (p: AnyProps & { label?: unknown; description?: unknown }) =>
      createElement("div", { "data-testid": "field" },
        createElement("span", { "data-testid": "field-label" }, p.label as never),
        createElement("span", { "data-testid": "field-desc" }, p.description as never),
        p.children as never,
      ),
    Focusable: passthrough("div"),
    PanelSection: passthrough("section"),
    PanelSectionRow: passthrough("div"),
    TextField: (p: AnyProps & { value?: string; onChange?: (e: unknown) => void }) =>
      createElement("input", {
        "data-testid": "text-field",
        value: p.value ?? "",
        onChange: (e: unknown) => p.onChange?.(e),
      }),
    ToggleField: (p: AnyProps & { checked?: boolean; onChange?: (v: boolean) => void; label?: unknown }) =>
      createElement(
        "div",
        { "data-testid": "toggle" },
        createElement("input", {
          type: "checkbox",
          "data-testid": "toggle-input",
          checked: p.checked ?? false,
          onChange: (e: { target: { checked: boolean } }) => p.onChange?.(e.target.checked),
        }),
        typeof p.label === "string" ? p.label : null,
      ),
    Dropdown: passthrough("select"),
    DropdownItem: (p: AnyProps) => createElement("select", {}, p.children as never),
    Spinner: () => createElement("div", { "data-testid": "spinner" }),
    showModal: vi.fn(),
    showContextMenu: vi.fn(),
    Menu: passthrough("div"),
    MenuItem: ({ children, onClick }: AnyProps) =>
      createElement("button", { onClick }, children as never),
    Router: { CloseSideMenus: vi.fn(), Navigate: vi.fn() },
    // findSP locates Steam's <SteamRoot> iframe document for stylesheet
    // injection. Tests run in happy-dom — no Steam, no iframe — so the
    // safe stub returns undefined and the consumer's `!sp?.window?.document`
    // guard short-circuits any DOM mutation.
    findSP: vi.fn(() => undefined),
    appActionButtonClasses: undefined,
    basicAppDetailsSectionStylerClasses: undefined,
  };
});
