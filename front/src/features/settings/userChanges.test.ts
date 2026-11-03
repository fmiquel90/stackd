import { describe, expect, it } from "vitest";
import { makeUser } from "@/lib/__fixtures__/factories";
import { describeChange, diffUser } from "./userChanges";

describe("staged user changes", () => {
  const bob = makeUser({
    email: "bob@dev.local",
    role: "writer",
    allowed_tiers: ["dev"],
    can_destroy: false,
  });

  it("keeps only real differences (tier order does not count)", () => {
    expect(diffUser(bob, { role: "writer", allowed_tiers: ["dev"] })).toBeNull();
    const two = makeUser({ allowed_tiers: ["dev", "prod"] });
    expect(diffUser(two, { allowed_tiers: ["prod", "dev"] })).toBeNull();
    expect(diffUser(bob, { role: "approver", can_destroy: false })).toEqual({ role: "approver" });
  });

  it("summarises each change in words", () => {
    const change = diffUser(bob, { role: "approver", allowed_tiers: ["prod"], can_destroy: true })!;
    expect(describeChange(bob, change)).toEqual([
      "bob: role writer → approver",
      "bob: + prod in allowed tiers",
      "bob: − dev from allowed tiers",
      "bob: may now destroy",
    ]);
  });
});
