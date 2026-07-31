import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
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
  },
  User: {
    StartRestart: vi.fn(),
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
    callable: <T>(_name: string) => vi.fn().mockResolvedValue(undefined) as unknown as T,
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
  const passthrough = (tag: string) => (props: AnyProps) => createElement(tag, props, props.children as never);
  return {
    ConfirmModal: passthrough("div"),
    ModalRoot: passthrough("div"),
    DialogButton: ({
      children,
      onClick,
      disabled,
      className,
      ...rest
    }: AnyProps & { disabled?: boolean; className?: string }) =>
      // Forward className + the a11y/identity attrs (aria-label, title, …) so
      // icon-only buttons (no text child) stay queryable in tests. style and
      // the FooterLegend-only props are dropped — no DOM effect under happy-dom.
      createElement(
        "button",
        {
          onClick,
          disabled,
          className,
          "aria-label": rest["aria-label"],
          title: rest.title,
        },
        children as never,
      ),
    DialogButtonPrimary: ({ children, onClick }: AnyProps) => createElement("button", { onClick }, children as never),
    // The optional `description` renders into a sibling span (mirroring Field), so a
    // test can assert on the description a ButtonItem shows — including its ABSENCE,
    // which a dropped prop would make vacuously true.
    ButtonItem: ({
      children,
      onClick,
      disabled,
      description,
    }: AnyProps & { onClick?: () => void; disabled?: boolean; description?: unknown }) =>
      createElement(
        "div",
        null,
        createElement("button", { onClick, disabled }, children as never),
        description == null ? null : createElement("span", { "data-testid": "button-desc" }, description as never),
      ),
    Field: (p: AnyProps & { label?: unknown; description?: unknown }) =>
      createElement(
        "div",
        { "data-testid": "field" },
        createElement("span", { "data-testid": "field-label" }, p.label as never),
        createElement("span", { "data-testid": "field-desc" }, p.description as never),
        p.children as never,
      ),
    // Focusable forwards onButtonDown as a real DOM "decky-button-down"
    // listener so tests can drive gamepad input via
    // fireEvent(el, new CustomEvent("decky-button-down", { detail: { button } })).
    // Other FooterLegend-only props (flow-children, actionDescriptionMap, …)
    // are dropped — they have no DOM effect under happy-dom.
    Focusable: ({
      children,
      style,
      onButtonDown,
      role,
      tabIndex,
      "aria-label": ariaLabel,
    }: AnyProps & { style?: unknown; onButtonDown?: (evt: unknown) => void }) =>
      createElement(
        "div",
        {
          "data-testid": "focusable",
          style,
          role,
          tabIndex,
          "aria-label": ariaLabel,
          ref: (el: HTMLDivElement | null) => {
            if (!el) return;
            const prev = (el as unknown as { _deckyButtonDown?: EventListener })._deckyButtonDown;
            if (prev) el.removeEventListener("decky-button-down", prev);
            if (!onButtonDown) return;
            const listener = ((e: Event) => onButtonDown(e)) as EventListener;
            (el as unknown as { _deckyButtonDown?: EventListener })._deckyButtonDown = listener;
            el.addEventListener("decky-button-down", listener);
          },
        },
        children as ReactNode,
      ),
    GamepadButton: {
      OK: 1,
      CANCEL: 2,
      SECONDARY: 3,
      TRIGGER_RIGHT: 8,
      DIR_UP: 9,
      DIR_DOWN: 10,
    },
    PanelSection: passthrough("section"),
    PanelSectionRow: passthrough("div"),
    TextField: (p: AnyProps & { value?: string; onChange?: (e: unknown) => void; onKeyDown?: (e: unknown) => void }) =>
      createElement("input", {
        "data-testid": "text-field",
        value: p.value ?? "",
        onChange: (e: unknown) => p.onChange?.(e),
        onKeyDown: (e: unknown) => p.onKeyDown?.(e),
      }),
    ToggleField: (
      p: AnyProps & { checked?: boolean; onChange?: (v: boolean) => void; label?: unknown; description?: unknown },
    ) =>
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
        // Mirrors the ButtonItem stub: a toggle's description carries real
        // user-facing copy, so it has to be assertable rather than dropped.
        p.description == null ? null : createElement("span", { "data-testid": "toggle-desc" }, p.description as never),
      ),
    Dropdown: passthrough("select"),
    DropdownItem: (p: AnyProps) => createElement("select", {}, p.children as never),
    // Per-prop testids so bar wiring (nProgress / indeterminate) is assertable
    // without a local re-mock; mirrors DownloadProgressRow's own stub.
    ProgressBar: (p: AnyProps & { nProgress?: number; indeterminate?: boolean }) =>
      createElement(
        "div",
        { "data-testid": "progress" },
        createElement("span", { "data-testid": "progress-progress" }, String(p.nProgress)),
        createElement("span", { "data-testid": "progress-indeterminate" }, String(p.indeterminate)),
      ),
    Spinner: () => createElement("div", { "data-testid": "spinner" }),
    showModal: vi.fn(),
    showContextMenu: vi.fn(),
    Menu: passthrough("div"),
    // `disabled` is forwarded: dropping it would let a test click an item the
    // component deliberately disabled and assert the resulting call, i.e. pass
    // against behavior the real UI does not have.
    MenuItem: ({ children, onClick, disabled }: AnyProps) =>
      createElement("button", { type: "button", onClick, disabled }, children as never),
    Navigation: { NavigateToExternalWeb: vi.fn(), Navigate: vi.fn() },
    // findSP locates Steam's <SteamRoot> iframe document for stylesheet
    // injection. Tests run in happy-dom — no Steam, no iframe — so the
    // safe stub returns undefined and the consumer's `!sp?.window?.document`
    // guard short-circuits any DOM mutation.
    findSP: vi.fn(() => undefined),
    appActionButtonClasses: undefined,
    basicAppDetailsSectionStylerClasses: undefined,
    appDetailsClasses: undefined,
    playSectionClasses: undefined,
  };
});
