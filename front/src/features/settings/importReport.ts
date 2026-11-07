import type {
  ConfigDocument,
  ImportChange,
  ImportMissingSecret,
  ImportOp,
  ImportSkipped,
  ImportWarning,
} from "@/api/types";

// Reading a configuration import (SPECS §22.3, guide backup-restore.md "Reading an import report"):
// the file is parsed in memory, and the API's report is regrouped and put into words.

/** The file the user picked, parsed — or why it is not a configuration document. */
export type ParsedFile = { ok: true; doc: ConfigDocument } | { ok: false; error: string };

export function parseConfigFile(text: string): ParsedFile {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch (err) {
    return { ok: false, error: `Not valid JSON: ${(err as Error).message}` };
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc))
    return { ok: false, error: "Not a configuration document: expected a JSON object." };
  const format = (doc as { format?: unknown }).format;
  if (format !== undefined && format !== "stackd.config")
    return { ok: false, error: `Not a Stackd configuration: format is ${JSON.stringify(format)}.` };
  return { ok: true, doc: doc as ConfigDocument };
}

export interface DocOverview {
  /** The space it was exported from, when the document says. */
  source: string | null;
  exportedAt: string | null;
  stacks: number;
  environments: number;
  variableSets: number;
  dependencies: number;
}

const len = (v: unknown) => (Array.isArray(v) ? v.length : 0);

export function docOverview(doc: ConfigDocument): DocOverview {
  const stacks = Array.isArray(doc.stacks) ? (doc.stacks as { environments?: unknown }[]) : [];
  const source = (doc.source as { space?: unknown } | null | undefined)?.space;
  return {
    source: typeof source === "string" ? source : null,
    exportedAt: typeof doc.exported_at === "string" ? doc.exported_at : null,
    stacks: stacks.length,
    environments: stacks.reduce((n, s) => n + len(s?.environments), 0),
    variableSets: len(doc.variable_sets),
    dependencies: len(doc.dependencies),
  };
}

/** Order of the kinds in a report: the order the import writes them (tiers first, edges last). */
const KIND_ORDER = [
  "tier",
  "secret_source",
  "worker_pool",
  "stack",
  "environment",
  "variable_set",
  "attachment",
  "variable",
  "hook",
  "notification_target",
  "cloud_integration",
  "dependency",
];

const KIND_LABEL: Record<string, [string, string]> = {
  tier: ["tier", "tiers"],
  secret_source: ["secret source", "secret sources"],
  worker_pool: ["worker pool", "worker pools"],
  stack: ["stack", "stacks"],
  environment: ["environment", "environments"],
  variable_set: ["variable set", "variable sets"],
  attachment: ["attachment", "attachments"],
  variable: ["variable", "variables"],
  hook: ["hook", "hooks"],
  notification_target: ["notification target", "notification targets"],
  cloud_integration: ["cloud integration", "cloud integrations"],
  dependency: ["dependency", "dependencies"],
};

export function kindLabel(kind: string, count = 1): string {
  const l = KIND_LABEL[kind];
  if (!l) return kind.replace(/_/g, " ");
  return count === 1 ? l[0] : l[1];
}

export interface KindGroup {
  kind: string;
  items: ImportChange[];
}

export interface OpGroup {
  op: ImportOp;
  count: number;
  kinds: KindGroup[];
}

const OPS: ImportOp[] = ["created", "updated", "unchanged"];

function kindRank(kind: string): number {
  const i = KIND_ORDER.indexOf(kind);
  return i === -1 ? KIND_ORDER.length : i;
}

/** Changes grouped by op (created, updated, unchanged — empty ops left out), then by kind. */
export function groupChanges(changes: ImportChange[]): OpGroup[] {
  return OPS.flatMap((op) => {
    const ofOp = changes.filter((c) => c.op === op);
    if (ofOp.length === 0) return [];
    const byKind = new Map<string, ImportChange[]>();
    for (const c of ofOp) byKind.set(c.kind, [...(byKind.get(c.kind) ?? []), c]);
    const kinds = [...byKind.entries()]
      .sort(([a], [b]) => kindRank(a) - kindRank(b) || a.localeCompare(b))
      .map(([kind, items]) => ({ kind, items: [...items].sort((x, y) => x.path.localeCompare(y.path)) }));
    return [{ op, count: ofOp.length, kinds }];
  });
}

const SKIP_REASON: Record<string, string> = {
  value_required:
    "sensitive value missing from the file and absent on the target — set it in the UI, or fill it in the file and re-import",
  secret_source_missing: "its secret source does not exist here — create it first, then re-import",
  bootstrap_secret_required:
    "a secret source needs its credential — create it on this instance first, then re-import",
  instance_admin_required: "needs an instance admin",
  target_not_found: "the stack, environment or tier it names is not here",
  downstream_not_found: "its downstream environment is not here",
  upstream_not_found: "its upstream environment is not found",
  upstream_forbidden: "its upstream is in a space you cannot write to",
  ambiguous_name: "two objects share this name on the target",
  name_taken_in_another_space: "this name is used by another space",
};

export function skipReason(s: Pick<ImportSkipped, "reason">): string {
  return SKIP_REASON[s.reason] ?? s.reason.replace(/_/g, " ");
}

const MISSING_ITEM: Record<string, string> = {
  variable: "sensitive variable value",
  repo_secret: "repository token or deploy key",
  webhook_secret: "webhook secret",
  bootstrap_secret: "secret source credential",
  secret_fallback: "static fallback value (imported as fail-closed “error” until set)",
};

export function missingItem(m: Pick<ImportMissingSecret, "item">): string {
  return MISSING_ITEM[m.item] ?? m.item.replace(/_/g, " ");
}

export function warningText(w: ImportWarning): string {
  switch (w.kind) {
    case "tier_differs":
      return `the tier differs on this instance${w.fields?.length ? ` (${w.fields.join(", ")})` : ""} — an import never changes an existing tier`;
    case "worker_pool_labels_differ":
      return "the worker pool's labels differ — an import never changes an existing pool";
    case "autodeploy_off":
      return "autodeploy left off until the missing secrets are filled";
    default:
      return [w.kind.replace(/_/g, " "), w.reason?.replace(/_/g, " ")].filter(Boolean).join(": ");
  }
}
