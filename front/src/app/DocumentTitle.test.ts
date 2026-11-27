import { describe, expect, it } from "vitest";
import { composeTitle, routeTitle, runStatusWord } from "./DocumentTitle";

describe("document title (DESIGN §4.1)", () => {
  it("puts the most specific name first and the brand last", () => {
    expect(composeTitle({ subject: "demo-network/prod", context: "demo" })).toBe(
      "demo-network/prod · demo — StackD",
    );
    expect(composeTitle({})).toBe("StackD");
  });

  it("prefixes the needs-you count only when there is one", () => {
    expect(composeTitle({ subject: "Overview", context: "demo", needsYou: 3 })).toBe(
      "(3) Overview · demo — StackD",
    );
    expect(composeTitle({ subject: "Queue", needsYou: 0 })).toBe("Queue — StackD");
  });

  it("names each route from its URL", () => {
    expect(routeTitle("/s/demo")).toEqual({ subject: "Overview", context: "demo" });
    expect(routeTitle("/s/demo/stacks/demo-network")).toEqual({ subject: "demo-network", context: "demo" });
    expect(routeTitle("/s/demo/stacks/demo-network/envs/prod")).toEqual({
      subject: "demo-network/prod",
      context: "demo",
    });
    expect(routeTitle("/s/demo/graph")).toEqual({ subject: "Graph", context: "demo" });
    expect(routeTitle("/s/demo/variable-sets/aws%20base")).toEqual({
      subject: "aws base",
      context: "Variable sets",
    });
    expect(routeTitle("/queue")).toEqual({ subject: "Queue" });
    expect(routeTitle("/")).toEqual({});
  });

  it("words a run's status like its badge", () => {
    expect(runStatusWord({ state: "unconfirmed", type: "tracked", confirmed_at: null })).toBe("unconfirmed");
    expect(runStatusWord({ state: "finished", type: "tracked", confirmed_at: null })).toBe("no changes");
    expect(runStatusWord({ state: "finished", type: "tracked", confirmed_at: "2026-11-27T10:00:00Z" })).toBe(
      "applied",
    );
    expect(
      runStatusWord({ state: "failed", type: "tracked", confirmed_at: "2026-11-27T10:00:00Z", error: null }),
    ).toBe("failed during apply");
  });
});
