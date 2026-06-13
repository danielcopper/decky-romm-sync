import { describe, it, expect } from "vitest";
import { trimServerUrl, isValidServerUrl, isHttpsUrl } from "./serverUrl";

describe("trimServerUrl", () => {
  it("strips surrounding whitespace", () => {
    expect(trimServerUrl("  https://romm.local  ")).toBe("https://romm.local");
  });

  it("leaves a clean URL unchanged", () => {
    expect(trimServerUrl("https://romm.local")).toBe("https://romm.local");
  });

  it("strips tabs and newlines", () => {
    expect(trimServerUrl("\t https://romm.local \n")).toBe("https://romm.local");
  });
});

describe("isValidServerUrl", () => {
  it.each(["http://romm.local", "https://romm.local", "https://romm.local:8443/romm", "  http://romm.local  "])(
    "accepts %s",
    (url) => {
      expect(isValidServerUrl(url)).toBe(true);
    },
  );

  it.each(["romm.local", "ftp://romm.local", "", "   ", "https://", "http://"])("rejects %s", (url) => {
    expect(isValidServerUrl(url)).toBe(false);
  });

  it("is case-insensitive on the scheme", () => {
    expect(isValidServerUrl("HTTPS://romm.local")).toBe(true);
  });
});

describe("isHttpsUrl", () => {
  it("is true for https URLs", () => {
    expect(isHttpsUrl("https://romm.local")).toBe(true);
  });

  it("is false for http URLs", () => {
    expect(isHttpsUrl("http://romm.local")).toBe(false);
  });

  it("trims before checking, so a leading space does not hide it", () => {
    expect(isHttpsUrl("  https://romm.local")).toBe(true);
  });

  it("is false for empty input", () => {
    expect(isHttpsUrl("")).toBe(false);
  });

  it("is case-insensitive", () => {
    expect(isHttpsUrl("HTTPS://romm.local")).toBe(true);
  });
});
