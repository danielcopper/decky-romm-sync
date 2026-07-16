import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { DownloadProgressRow } from "./DownloadProgressRow";

// Local @decky/ui mock adds ProgressBar (not in the global stub) and exposes
// per-prop testids so the bar wiring (nProgress / indeterminate) can be asserted
// directly. Mirrors the DownloadQueue test mock.
vi.mock("@decky/ui", async () => {
  type AnyProps = Record<string, unknown> & { children?: unknown };
  const { createElement: ce } = await import("react");
  return {
    PanelSectionRow: (p: AnyProps) => ce("div", p, p.children as never),
    ProgressBar: (p: AnyProps & { nProgress?: number; indeterminate?: boolean }) =>
      ce(
        "div",
        { "data-testid": "progress" },
        ce("span", { "data-testid": "progress-progress" }, String(p.nProgress)),
        ce("span", { "data-testid": "progress-indeterminate" }, String(p.indeterminate)),
      ),
  };
});

describe("DownloadProgressRow", () => {
  it("total > 0: nProgress is (bytes/total)*100, indeterminate=false, bytes read 'X / Y'", () => {
    const { container } = render(<DownloadProgressRow caption="Sonic" bytesDownloaded={256} totalBytes={1024} />);
    expect(container.querySelector('[data-testid="progress-progress"]')?.textContent).toBe("25");
    expect(container.querySelector('[data-testid="progress-indeterminate"]')?.textContent).toBe("false");
    expect(container.querySelector('[data-testid="dl-bytes"]')?.textContent).toBe("256 B / 1.0 KB");
  });

  it("total === 0: nProgress undefined, indeterminate=true, bytes read the downloaded count alone", () => {
    const { container } = render(<DownloadProgressRow caption="Sonic" bytesDownloaded={700} totalBytes={0} />);
    expect(container.querySelector('[data-testid="progress-progress"]')?.textContent).toBe("undefined");
    expect(container.querySelector('[data-testid="progress-indeterminate"]')?.textContent).toBe("true");
    expect(container.querySelector('[data-testid="dl-bytes"]')?.textContent).toBe("700 B");
  });

  it("renders a plain-string caption in the dl-caption span", () => {
    const { container } = render(<DownloadProgressRow caption="Chrono" bytesDownloaded={0} totalBytes={100} />);
    expect(container.querySelector('[data-testid="dl-caption"]')?.textContent).toBe("Chrono");
  });

  it("renders a composed ReactNode caption verbatim (name, platform, status suffix)", () => {
    const { container } = render(
      <DownloadProgressRow caption={<>Kirby (Genesis){" — Paused"}</>} bytesDownloaded={0} totalBytes={100} />,
    );
    expect(container.querySelector('[data-testid="dl-caption"]')?.textContent).toBe("Kirby (Genesis) — Paused");
  });
});
