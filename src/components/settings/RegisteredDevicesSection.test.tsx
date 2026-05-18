import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render } from "@testing-library/react";
import { createElement } from "react";
import { RegisteredDevicesSection } from "./RegisteredDevicesSection";
import type { RegisteredDevice } from "../../types";

// Local re-mock: Field renders both label+description so we can assert the
// pipe-separated parts string. PanelSection/PanelSectionRow are pass-through.
type AnyProps = Record<string, unknown> & { children?: unknown };
vi.mock("@decky/ui", () => ({
  PanelSection: (p: AnyProps) =>
    createElement("section", {}, p.children as never),
  PanelSectionRow: (p: AnyProps) =>
    createElement("div", { "data-testid": "row" }, p.children as never),
  Field: (p: AnyProps & { label?: unknown; description?: unknown }) =>
    createElement(
      "div",
      { "data-testid": "field" },
      createElement("span", { "data-testid": "field-label" }, p.label as never),
      createElement("span", { "data-testid": "field-desc" }, p.description as never),
    ),
}));

function makeDevice(overrides: Partial<RegisteredDevice> = {}): RegisteredDevice {
  return {
    id: "d1abc12345678",
    name: "Steam Deck",
    platform: "linux",
    client: "decky-romm-sync",
    client_version: "0.17.1",
    last_seen: "2025-06-15T11:55:00Z",
    created_at: "2025-06-01T10:00:00Z",
    is_current_device: false,
    ...overrides,
  };
}

function defaultProps(
  overrides: Partial<React.ComponentProps<typeof RegisteredDevicesSection>> = {},
) {
  return {
    devicesLoading: false,
    devicesError: null,
    registeredDevices: null,
    ...overrides,
  };
}

describe("RegisteredDevicesSection", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-06-15T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  describe("loading state", () => {
    it("renders 'Loading...' when devicesLoading is true", () => {
      const { getAllByTestId } = render(
        <RegisteredDevicesSection {...defaultProps({ devicesLoading: true })} />,
      );
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("Loading...");
    });

    it("hides loading even if devicesError is also set (loading takes precedence)", () => {
      const { getAllByTestId } = render(
        <RegisteredDevicesSection
          {...defaultProps({ devicesLoading: true, devicesError: "boom" })}
        />,
      );
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("Loading...");
      expect(labels).not.toContain("Could not load devices");
    });
  });

  describe("error state", () => {
    it("renders 'Could not load devices' with the error in the description", () => {
      const { getAllByTestId } = render(
        <RegisteredDevicesSection
          {...defaultProps({ devicesError: "Network unreachable" })}
        />,
      );
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(labels).toContain("Could not load devices");
      expect(descs).toContain("Network unreachable");
    });
  });

  describe("empty state", () => {
    it("renders 'No devices registered' when the list is empty", () => {
      const { getAllByTestId } = render(
        <RegisteredDevicesSection
          {...defaultProps({ registeredDevices: [] })}
        />,
      );
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("No devices registered");
    });

    it("renders nothing meaningful when registeredDevices is null and no loading/error", () => {
      const { queryAllByTestId } = render(<RegisteredDevicesSection {...defaultProps()} />);
      expect(queryAllByTestId("field")).toHaveLength(0);
    });
  });

  describe("populated list", () => {
    it("renders one row per device", () => {
      const devices = [
        makeDevice({ id: "device-aaaaaaaa", name: "Steam Deck" }),
        makeDevice({ id: "device-bbbbbbbb", name: "Desktop" }),
      ];
      const { getAllByTestId } = render(
        <RegisteredDevicesSection {...defaultProps({ registeredDevices: devices })} />,
      );
      // 2 device rows; no loading/error/empty rows are rendered.
      expect(getAllByTestId("field")).toHaveLength(2);
    });

    it("formats the description as 'client vX · platform · last seen Xm ago · ID 8chars'", () => {
      const device = makeDevice({
        id: "device-aaaaaaaa-rest-of-uuid",
        client: "decky-romm-sync",
        client_version: "0.17.1",
        platform: "linux",
        last_seen: "2025-06-15T11:55:00Z", // 5 minutes ago
      });
      const { getAllByTestId } = render(
        <RegisteredDevicesSection
          {...defaultProps({ registeredDevices: [device] })}
        />,
      );
      const desc = getAllByTestId("field-desc")[0]?.textContent ?? "";
      expect(desc).toContain("decky-romm-sync v0.17.1");
      expect(desc).toContain("linux");
      expect(desc).toContain("last seen 5m ago");
      expect(desc).toContain("ID device-a"); // first 8 chars of id
    });

    it("omits the platform segment when device.platform is null", () => {
      const device = makeDevice({ platform: null });
      const { getAllByTestId } = render(
        <RegisteredDevicesSection
          {...defaultProps({ registeredDevices: [device] })}
        />,
      );
      const desc = getAllByTestId("field-desc")[0]?.textContent ?? "";
      // No "linux" or platform string — segments are: client, last seen, ID.
      const segments = desc.split(" · ");
      expect(segments).toHaveLength(3);
    });

    it("falls back to 'unknown client v?' when client/client_version are null", () => {
      const device = makeDevice({ client: null, client_version: null });
      const { getAllByTestId } = render(
        <RegisteredDevicesSection
          {...defaultProps({ registeredDevices: [device] })}
        />,
      );
      const desc = getAllByTestId("field-desc")[0]?.textContent ?? "";
      expect(desc).toContain("unknown client v?");
    });

    it("renders '(unnamed)' when device.name is null", () => {
      const device = makeDevice({ name: null });
      const { container } = render(
        <RegisteredDevicesSection
          {...defaultProps({ registeredDevices: [device] })}
        />,
      );
      expect(container.textContent).toContain("(unnamed)");
    });

    it("renders the '(this device)' badge only on the current device", () => {
      const devices = [
        makeDevice({ id: "device-aaaa1111", name: "Steam Deck", is_current_device: true }),
        makeDevice({ id: "device-bbbb2222", name: "Desktop", is_current_device: false }),
      ];
      const { container } = render(
        <RegisteredDevicesSection {...defaultProps({ registeredDevices: devices })} />,
      );
      const matches = container.textContent?.match(/\(this device\)/g) ?? [];
      expect(matches).toHaveLength(1);
    });

    it("renders 'ID —' when the id is empty string", () => {
      const device = makeDevice({ id: "" });
      const { getAllByTestId } = render(
        <RegisteredDevicesSection
          {...defaultProps({ registeredDevices: [device] })}
        />,
      );
      const desc = getAllByTestId("field-desc")[0]?.textContent ?? "";
      expect(desc).toContain("ID —");
    });
  });
});
