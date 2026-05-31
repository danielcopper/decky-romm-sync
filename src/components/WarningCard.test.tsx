import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { WarningCard } from "./WarningCard";

describe("WarningCard", () => {
  it("renders title and message", () => {
    render(<WarningCard title="Heads up" message="Something happened" />);
    expect(screen.getByText("Heads up")).toBeInTheDocument();
    expect(screen.getByText("Something happened")).toBeInTheDocument();
  });

  it("applies compact padding when compact=true", () => {
    const { container } = render(<WarningCard title="t" message="m" compact />);
    const root = container.firstChild as HTMLElement;
    expect(root).toHaveStyle({ padding: "24px 16px" });
  });

  it("uses spacious padding by default", () => {
    const { container } = render(<WarningCard title="t" message="m" />);
    const root = container.firstChild as HTMLElement;
    expect(root).toHaveStyle({ padding: "40px 32px" });
  });
});
