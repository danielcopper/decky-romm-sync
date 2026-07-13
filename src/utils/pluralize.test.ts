import { describe, it, expect } from "vitest";
import { pluralize } from "./pluralize";

describe("pluralize", () => {
  it("keeps the singular form for exactly 1", () => {
    expect(pluralize(1, "game")).toBe("1 game");
    expect(pluralize(1, "platform")).toBe("1 platform");
    expect(pluralize(1, "collection")).toBe("1 collection");
  });

  it("appends 's' for 0 and for counts greater than 1", () => {
    expect(pluralize(0, "game")).toBe("0 games");
    expect(pluralize(2, "platform")).toBe("2 platforms");
    expect(pluralize(2200, "game")).toBe("2200 games");
  });
});
