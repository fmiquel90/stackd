import { describe, expect, it } from "vitest";
import { parseLabels } from "./NewEnvironmentDialog";

describe("parseLabels", () => {
  it("reads comma-separated key=value pairs", () => {
    expect(parseLabels("region=eu-west-3, team = core")).toEqual({
      labels: { region: "eu-west-3", team: "core" },
      errors: [],
    });
  });
  it("keeps everything after the first = in the value", () => {
    expect(parseLabels("selector=a=b")).toEqual({ labels: { selector: "a=b" }, errors: [] });
  });
  it("is undefined when empty", () => {
    expect(parseLabels("  ")).toEqual({ labels: undefined, errors: [] });
    expect(parseLabels(" , ")).toEqual({ labels: undefined, errors: [] });
  });
  it("reports malformed pairs instead of dropping them", () => {
    const parsed = parseLabels("region=eu-west-3,oops,=x,team=,region=us-east-1");
    expect(parsed.labels).toEqual({ region: "eu-west-3" });
    expect(parsed.errors).toEqual([
      '"oops" is not key=value',
      '"=x" has no key',
      '"team=" has no value',
      '"region" is set twice',
    ]);
  });
});
