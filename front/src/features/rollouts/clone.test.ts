import { describe, expect, it } from "vitest";
import { parseLabels } from "./NewEnvironmentDialog";

describe("parseLabels", () => {
  it("reads comma-separated key=value pairs", () => {
    expect(parseLabels("region=eu-west-3, team = core")).toEqual({ region: "eu-west-3", team: "core" });
  });
  it("is undefined when empty and skips malformed pairs", () => {
    expect(parseLabels("  ")).toBeUndefined();
    expect(parseLabels("region=eu-west-3,oops")).toEqual({ region: "eu-west-3" });
  });
});
