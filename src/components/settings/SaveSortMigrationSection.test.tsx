import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { SaveSortMigrationSection } from "./SaveSortMigrationSection";
import type { SaveSortMigrationStatus } from "../../types";

// Local re-mock: ButtonItem must forward `disabled` so we can assert the
// disabled state while migrating.
type AnyProps = Record<string, unknown> & { children?: unknown };
vi.mock("@decky/ui", () => ({
  PanelSection: (p: AnyProps) => createElement("section", {}, p.children as never),
  PanelSectionRow: (p: AnyProps) => createElement("div", {}, p.children as never),
  Field: (p: AnyProps & { label?: unknown }) => createElement("div", { "data-testid": "field" }, p.label as never),
  ButtonItem: ({
    children,
    onClick,
    disabled,
  }: AnyProps & {
    onClick?: () => void;
    disabled?: boolean;
  }) => createElement("button", { onClick, disabled }, children as never),
}));

function makeMigration(overrides: Partial<SaveSortMigrationStatus> = {}): SaveSortMigrationStatus {
  return {
    pending: true,
    old_settings: { sort_by_content: false, sort_by_core: false },
    new_settings: { sort_by_content: true, sort_by_core: false },
    saves_count: 12,
    ...overrides,
  };
}

function defaultProps(overrides: Partial<React.ComponentProps<typeof SaveSortMigrationSection>> = {}) {
  return {
    migration: makeMigration(),
    migrating: false,
    result: "",
    onMigrate: vi.fn(),
    onDismiss: vi.fn(),
    ...overrides,
  };
}

describe("SaveSortMigrationSection", () => {
  describe("banner content", () => {
    it("renders the warning headline", () => {
      const { container } = render(<SaveSortMigrationSection {...defaultProps()} />);
      expect(container.textContent).toContain("RetroArch save sorting changed");
    });

    it("renders the 'From:' line when old_settings is present", () => {
      const { container } = render(<SaveSortMigrationSection {...defaultProps()} />);
      expect(container.textContent).toContain("From: Sort by content: OFF, Sort by core: OFF");
    });

    it("renders the 'To:' line when new_settings is present", () => {
      const { container } = render(<SaveSortMigrationSection {...defaultProps()} />);
      expect(container.textContent).toContain("To: Sort by content: ON, Sort by core: OFF");
    });

    it("omits the 'From:' line when old_settings is missing", () => {
      const { container } = render(
        <SaveSortMigrationSection {...defaultProps({ migration: makeMigration({ old_settings: undefined }) })} />,
      );
      expect(container.textContent).not.toContain("From:");
    });

    it("omits the 'To:' line when new_settings is missing", () => {
      const { container } = render(
        <SaveSortMigrationSection {...defaultProps({ migration: makeMigration({ new_settings: undefined }) })} />,
      );
      expect(container.textContent).not.toContain("To:");
    });

    it("renders the saves_count line", () => {
      const { container } = render(
        <SaveSortMigrationSection {...defaultProps({ migration: makeMigration({ saves_count: 7 }) })} />,
      );
      expect(container.textContent).toContain("7 save file(s) to migrate");
    });

    it("falls back to '0 save file(s) to migrate' when saves_count is missing", () => {
      const { container } = render(
        <SaveSortMigrationSection {...defaultProps({ migration: makeMigration({ saves_count: undefined }) })} />,
      );
      expect(container.textContent).toContain("0 save file(s) to migrate");
    });
  });

  describe("buttons", () => {
    it("calls onMigrate when 'Migrate Save Files' is clicked", () => {
      const onMigrate = vi.fn();
      const { getByText } = render(<SaveSortMigrationSection {...defaultProps({ onMigrate })} />);
      fireEvent.click(getByText("Migrate Save Files"));
      expect(onMigrate).toHaveBeenCalledTimes(1);
    });

    it("calls onDismiss when 'Dismiss (I migrated manually)' is clicked", () => {
      const onDismiss = vi.fn();
      const { getByText } = render(<SaveSortMigrationSection {...defaultProps({ onDismiss })} />);
      fireEvent.click(getByText("Dismiss (I migrated manually)"));
      expect(onDismiss).toHaveBeenCalledTimes(1);
    });

    it("renders 'Migrating...' label and disables both buttons while migrating=true", () => {
      const { getByText } = render(<SaveSortMigrationSection {...defaultProps({ migrating: true })} />);
      const migrateBtn = getByText("Migrating...");
      const dismissBtn = getByText("Dismiss (I migrated manually)");
      expect(migrateBtn).toBeDisabled();
      expect(dismissBtn).toBeDisabled();
    });

    it("renders 'Migrate Save Files' label and enables both buttons when migrating=false", () => {
      const { getByText } = render(<SaveSortMigrationSection {...defaultProps()} />);
      const migrateBtn = getByText("Migrate Save Files");
      const dismissBtn = getByText("Dismiss (I migrated manually)");
      expect(migrateBtn).not.toBeDisabled();
      expect(dismissBtn).not.toBeDisabled();
    });
  });

  describe("result row", () => {
    it("renders the result Field when result is non-empty", () => {
      const { getAllByTestId } = render(
        <SaveSortMigrationSection {...defaultProps({ result: "Migrated 12 files" })} />,
      );
      const labels = getAllByTestId("field").map((el) => el.textContent);
      expect(labels).toContain("Migrated 12 files");
    });

    it("omits the result Field when result is empty", () => {
      const { queryAllByTestId } = render(<SaveSortMigrationSection {...defaultProps()} />);
      expect(queryAllByTestId("field")).toHaveLength(0);
    });
  });
});
