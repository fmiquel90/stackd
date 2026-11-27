import { describe, expect, it } from "vitest";
import { shortId } from "./ids";

describe("shortId", () => {
  it("keeps the random tail of a UUIDv7, not its timestamp prefix", () => {
    // Two runs created a few seconds apart share the first eight hex characters.
    const a = "01a1276c-e6dd-7d3d-bd8f-7a969ce3880b";
    const b = "01a1276c-f012-7a01-9c3e-11d0a4b7c2e9";
    expect(shortId(a)).toBe("9ce3880b");
    expect(shortId(b)).toBe("a4b7c2e9");
    expect(shortId(a)).not.toBe(shortId(b));
  });

  it("truncates a value that is not a canonical UUID", () => {
    expect(shortId("01a1276cabcd")).toBe("01a1276c");
    expect(shortId("abc")).toBe("abc");
  });
});
