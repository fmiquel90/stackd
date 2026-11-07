import { describe, expect, it } from "vitest";
import { rolesFor } from "./access";
import { SERVICE_ACCOUNT_NAME, daysLeft, parseDays, tokenStatus } from "./serviceAccountModel";

const NOW = new Date("2026-11-06T12:00:00Z");

describe("service account rules", () => {
  it("matches the API's name pattern", () => {
    expect(SERVICE_ACCOUNT_NAME.test("ci-export")).toBe(true);
    expect(SERVICE_ACCOUNT_NAME.test("0ci")).toBe(true);
    expect(SERVICE_ACCOUNT_NAME.test("-ci")).toBe(false);
    expect(SERVICE_ACCOUNT_NAME.test("CI")).toBe(false);
    expect(SERVICE_ACCOUNT_NAME.test("a".repeat(64))).toBe(false);
  });

  it("offers a service account reader or writer only at instance level", () => {
    expect(rolesFor("service", "instance", "reader")).toEqual(["reader", "writer"]);
    // Up to a space's admin through its memberships (SPECS §23).
    expect(rolesFor("service", "space", "reader")).toEqual(["reader", "writer", "approver", "admin"]);
    expect(rolesFor("human", "instance", "reader")).toContain("admin");
    // A role the account somehow holds stays visible rather than silently replaced.
    expect(rolesFor("service", "instance", "admin")).toEqual(["reader", "writer", "admin"]);
  });
});

describe("token status", () => {
  it("is revoked, expired or active", () => {
    expect(tokenStatus({ expires_at: "2027-01-01T00:00:00Z", revoked_at: "2026-11-01T00:00:00Z" }, NOW)).toBe(
      "revoked",
    );
    expect(tokenStatus({ expires_at: "2026-11-06T11:59:59Z", revoked_at: null }, NOW)).toBe("expired");
    expect(tokenStatus({ expires_at: "2026-11-07T00:00:00Z", revoked_at: null }, NOW)).toBe("active");
  });

  it("counts whole days left", () => {
    expect(daysLeft("2026-11-16T12:00:00Z", NOW)).toBe(10);
    expect(daysLeft("2026-11-06T18:00:00Z", NOW)).toBe(0);
    expect(daysLeft("2026-11-01T00:00:00Z", NOW)).toBe(0);
  });

  it("accepts 1 to 365 days only", () => {
    expect(parseDays("90")).toBe(90);
    expect(parseDays(" 365 ")).toBe(365);
    expect(parseDays("0")).toBeNull();
    expect(parseDays("366")).toBeNull();
    expect(parseDays("1.5")).toBeNull();
    expect(parseDays("")).toBeNull();
  });
});
