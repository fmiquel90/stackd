import { describe, expect, it } from "vitest";
import type { ImportChange } from "@/api/types";
import {
  docOverview,
  groupChanges,
  kindLabel,
  missingItem,
  parseConfigFile,
  skipReason,
  warningText,
} from "./importReport";

describe("parseConfigFile", () => {
  it("accepts a stackd.config object, with or without its format line", () => {
    expect(parseConfigFile('{"format":"stackd.config","stacks":[]}')).toEqual({
      ok: true,
      doc: { format: "stackd.config", stacks: [] },
    });
    expect(parseConfigFile('{"stacks":[]}').ok).toBe(true);
  });

  it("says why a file is not a document", () => {
    const bad = parseConfigFile("{nope");
    expect(bad.ok).toBe(false);
    expect(!bad.ok && bad.error).toMatch(/^Not valid JSON/);
    expect(parseConfigFile("[1,2]")).toEqual({
      ok: false,
      error: "Not a configuration document: expected a JSON object.",
    });
    expect(parseConfigFile("null").ok).toBe(false);
    expect(parseConfigFile('{"format":"terraform.tfstate"}')).toEqual({
      ok: false,
      error: 'Not a Stackd configuration: format is "terraform.tfstate".',
    });
  });
});

describe("docOverview", () => {
  it("counts what the document carries", () => {
    expect(
      docOverview({
        source: { space: "payments" },
        exported_at: "2026-11-01T10:00:00Z",
        stacks: [{ environments: [{}, {}] }, { environments: [{}] }, {}],
        variable_sets: [{}],
        dependencies: [],
      }),
    ).toEqual({
      source: "payments",
      exportedAt: "2026-11-01T10:00:00Z",
      stacks: 3,
      environments: 3,
      variableSets: 1,
      dependencies: 0,
    });
    expect(docOverview({})).toMatchObject({ source: null, stacks: 0, environments: 0 });
  });
});

describe("groupChanges", () => {
  const c = (op: ImportChange["op"], kind: string, path: string, fields?: string[]): ImportChange => ({
    op,
    kind,
    path,
    ...(fields ? { fields } : {}),
  });

  it("groups by op (created, updated, unchanged), then by kind in the import's order", () => {
    const groups = groupChanges([
      c("unchanged", "tier", "tiers/prod"),
      c("created", "environment", "stacks/net/environments/prod"),
      c("updated", "stack", "stacks/net", ["repo_url"]),
      c("created", "stack", "stacks/net"),
      c("created", "environment", "stacks/net/environments/dev"),
      c("created", "zz_new_kind", "x"),
      c("created", "tier", "tiers/qa"),
    ]);
    expect(groups.map((g) => [g.op, g.count])).toEqual([
      ["created", 5],
      ["updated", 1],
      ["unchanged", 1],
    ]);
    expect(groups[0].kinds.map((k) => k.kind)).toEqual(["tier", "stack", "environment", "zz_new_kind"]);
    // Paths sorted within a kind.
    expect(groups[0].kinds[2].items.map((i) => i.path)).toEqual([
      "stacks/net/environments/dev",
      "stacks/net/environments/prod",
    ]);
    expect(groups[1].kinds[0].items[0].fields).toEqual(["repo_url"]);
  });

  it("leaves out empty ops", () => {
    expect(groupChanges([])).toEqual([]);
    expect(groupChanges([c("updated", "hook", "h")]).map((g) => g.op)).toEqual(["updated"]);
  });
});

describe("report wording", () => {
  it("names kinds, singular and plural", () => {
    expect(kindLabel("variable_set")).toBe("variable set");
    expect(kindLabel("dependency", 2)).toBe("dependencies");
    expect(kindLabel("brand_new")).toBe("brand new");
  });

  it("puts skip reasons, missing secrets and warnings into words", () => {
    expect(skipReason({ reason: "instance_admin_required" })).toBe("needs an instance admin");
    expect(skipReason({ reason: "something_else" })).toBe("something else");
    expect(missingItem({ item: "repo_secret" })).toBe("repository token or deploy key");
    expect(warningText({ kind: "tier_differs", fields: ["requires_four_eyes"] })).toMatch(
      /\(requires_four_eyes\) — an import never changes an existing tier/,
    );
    expect(warningText({ kind: "autodeploy_off", reason: "left_to_fill" })).toBe(
      "autodeploy left off until the missing secrets are filled",
    );
    expect(warningText({ kind: "new_warning", reason: "some_reason" })).toBe("new warning: some reason");
  });
});
