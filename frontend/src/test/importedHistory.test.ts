import { describe, it, expect } from "vitest";
import { historyAttribution, importedNote } from "../utils/importedHistory";

describe("importedNote (#1675)", () => {
  it("is null for a row that was not imported", () => {
    expect(importedNote(null)).toBeNull();
    expect(importedNote(undefined)).toBeNull();
  });

  it("names the source, or just says imported", () => {
    expect(importedNote("carol")).toBe("imported, originally carol");
    expect(importedNote("")).toBe("imported");
  });
});

describe("historyAttribution (#1675)", () => {
  it("is the plain by-line for a recorded row", () => {
    expect(historyAttribution("Kelly", null)).toBe("by Kelly");
    expect(historyAttribution(null, null)).toBeNull();
  });

  it("appends the note for an imported row", () => {
    expect(historyAttribution("Kelly", "carol")).toBe("by Kelly · imported, originally carol");
    expect(historyAttribution(null, "")).toBe("imported");
  });
});
