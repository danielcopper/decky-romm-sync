import { describe, it, expect, vi, beforeEach } from "vitest";
import { toaster } from "@decky/api";
import type { ToastNotification } from "@decky/api";
import { showToast, PLUGIN_NAME } from "./toast";

describe("showToast", () => {
  beforeEach(() => {
    vi.mocked(toaster.toast).mockReset();
  });

  it("titles the toast with the plugin name", () => {
    showToast("Sync complete");

    expect(toaster.toast).toHaveBeenCalledOnce();
    expect(toaster.toast).toHaveBeenCalledWith({ title: PLUGIN_NAME, body: "Sync complete" });
  });

  it("passes options through alongside the body", () => {
    showToast("12 games added", { duration: 8000, subtext: "3 skipped" });

    expect(toaster.toast).toHaveBeenCalledWith({
      title: PLUGIN_NAME,
      body: "12 games added",
      duration: 8000,
      subtext: "3 skipped",
    });
  });

  it("names the plugin the same way the manifest does", () => {
    expect(PLUGIN_NAME).toBe("Tender");
  });

  it("returns the notification so a caller can dismiss it", () => {
    const notification = { data: { title: PLUGIN_NAME, body: "Downloading" }, dismiss: vi.fn() };
    vi.mocked(toaster.toast).mockReturnValue(notification as ToastNotification);

    expect(showToast("Downloading")).toBe(notification);
  });
});
