import { describe, expect, it } from "vitest";
import { parseProvenance } from "./ProvenanceBadge";

// Pure resolution-provenance parsing (SPECS §3.4/§15.2). Order matters: secret_fallback/override
// must be matched before the shorter `secret:` prefix.
describe("parseProvenance", () => {
  it("parses each prefixed kind with its name", () => {
    expect(parseProvenance("set:common-aws")).toEqual({ kind: "set", name: "common-aws" });
    expect(parseProvenance("dependency:network/prod")).toEqual({
      kind: "dependency",
      name: "network/prod",
    });
    expect(parseProvenance("secret:db")).toEqual({ kind: "secret", name: "db" });
    expect(parseProvenance("secret_fallback:db")).toEqual({
      kind: "secret_fallback",
      name: "db",
    });
    expect(parseProvenance("secret_override:db")).toEqual({
      kind: "secret_override",
      name: "db",
    });
  });

  it("does not mistake secret_fallback/override for the bare secret prefix", () => {
    expect(parseProvenance("secret_fallback:x").kind).toBe("secret_fallback");
    expect(parseProvenance("secret_override:x").kind).toBe("secret_override");
  });

  it("parses bare layers", () => {
    expect(parseProvenance("mock")).toEqual({ kind: "mock" });
    expect(parseProvenance("env")).toEqual({ kind: "env" });
    expect(parseProvenance("stack")).toEqual({ kind: "stack" });
  });

  it("falls back to stack for an unknown token", () => {
    expect(parseProvenance("weird").kind).toBe("stack");
  });
});
