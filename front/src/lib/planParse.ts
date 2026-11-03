// Parse the human-readable plan (`tofu show -no-color`, streamed in the `plan` log section) into
// resources grouped by action (DESIGN §5.2). A stopgap until the API serves `resource_changes`
// (AUDIT A1): it is text parsing, so the contract is that NO line is ever dropped — every input line
// lands in exactly one bucket, and anything not understood is shown verbatim ("unparsed").

export type PlanAction = "destroy" | "replace" | "create" | "update" | "read" | "move" | "other";

export interface PlanResource {
  address: string;
  action: PlanAction;
  /** The tofu phrase after the address ("will be destroyed", "has changed"…). */
  phrase: string;
  /** `# (because …)` explanation lines under the header. */
  notes: string[];
  /** Body lines, from the `resource "…" {` opener to its closing brace, as printed. */
  body: string[];
  /** Top-level attributes with a change marker (`+ - ~ -/+ +/-`). */
  changedAttributes: string[];
  /** Attributes marked `# forces replacement`. */
  forcesReplacement: string[];
  /** An update whose changed attributes are all tags (`tags`, `tags_all`). */
  tagOnly: boolean;
  /** Line offset of the header in the input, for comment anchors and log links. */
  line: number;
}

export interface ParsedPlan {
  resources: PlanResource[];
  /** "Changes to Outputs:" section body. */
  outputs: string[];
  /** `Plan: 1 to add, …` / `No changes. …` */
  summary: string | null;
  /** Legend and banner lines before the resources (known tofu boilerplate). */
  preamble: string[];
  /** Lines the parser did not recognise — always shown, above the groups. */
  unparsed: string[];
}

/** Display order: destructive first (DESIGN §5.2). */
export const ACTION_ORDER: PlanAction[] = ["destroy", "replace", "create", "update", "read", "move", "other"];

const HEADER =
  /^ {2}# (\S.*?) (will be created|will be updated in-place|will be destroyed|must be replaced|will be replaced, as requested|is tainted, so must be replaced|will be read during apply|has moved to \S+|has changed|has been deleted|will no longer be managed by \S+)\s*$/;
const NOTE = /^ {2}# \(.*\)\s*$/;
// The opener is indented by two, except the 3-character markers (`-/+`, `+/-`) which start at col 0.
const BODY_OPEN = /^(?: {2}(?:[-+~]|<=)? *|-\/\+ |\+\/- )(?:resource|data) "[^"]+" "[^"]+" \{/;
const BODY_CLOSE = /^ {4}\}\s*$/;
const ATTR = /^ {6}([-+~]|-\/\+|\+\/-) +"?([\w-]+)"?\s*(?:=|\{|\[)/;
const TAGS = new Set(["tags", "tags_all"]);

const PREAMBLE = [
  /^(OpenTofu|Terraform) used the selected providers/,
  /^plan\. Resource actions are indicated with the following symbols:/,
  /^following execution plan\. Resource actions/,
  /^ {2}([-+~]|-\/\+|\+\/-|<=) [a-z]/, // legend lines: "  + create", "-/+ destroy and then create…"
  /^(OpenTofu|Terraform) will perform the following actions:/,
  /^(OpenTofu|Terraform) detected the following changes made outside/,
  /^Note: Objects have changed outside of/,
  /^Unless you have made equivalent changes/,
  /^the last "(tofu|terraform) apply" which may have affected this plan:/,
  /^\s*$/,
];

function actionOf(phrase: string): PlanAction {
  if (phrase === "will be created") return "create";
  if (phrase === "will be updated in-place") return "update";
  if (phrase === "will be destroyed" || phrase === "has been deleted") return "destroy";
  if (/replace/.test(phrase)) return "replace";
  if (phrase === "will be read during apply") return "read";
  if (phrase.startsWith("has moved to")) return "move";
  return "other";
}

export function parsePlan(lines: string[]): ParsedPlan {
  const out: ParsedPlan = { resources: [], outputs: [], summary: null, preamble: [], unparsed: [] };
  let i = 0;
  let inOutputs = false;

  while (i < lines.length) {
    const line = lines[i];
    const header = HEADER.exec(line);

    if (header) {
      inOutputs = false;
      const res: PlanResource = {
        address: header[1],
        action: actionOf(header[2]),
        phrase: header[2],
        notes: [],
        body: [],
        changedAttributes: [],
        forcesReplacement: [],
        tagOnly: false,
        line: i,
      };
      i++;
      while (i < lines.length && NOTE.test(lines[i])) res.notes.push(lines[i++].trim());
      if (i < lines.length && BODY_OPEN.test(lines[i])) {
        while (i < lines.length) {
          const b = lines[i++];
          res.body.push(b);
          const attr = ATTR.exec(b);
          if (attr) {
            res.changedAttributes.push(attr[2]);
            if (b.includes("# forces replacement")) res.forcesReplacement.push(attr[2]);
          }
          if (BODY_CLOSE.test(b)) break;
        }
      }
      res.tagOnly =
        res.action === "update" &&
        res.changedAttributes.length > 0 &&
        res.changedAttributes.every((a) => TAGS.has(a));
      out.resources.push(res);
      continue;
    }

    if (/^(Plan: \d+ to |No changes\.|Destroy: )/.test(line) || /^You can apply this plan/.test(line)) {
      out.summary = out.summary ? `${out.summary}\n${line}` : line;
      inOutputs = false;
    } else if (/^Changes to Outputs:/.test(line)) {
      inOutputs = true;
      out.outputs.push(line);
    } else if (inOutputs) {
      out.outputs.push(line);
    } else if (PREAMBLE.some((re) => re.test(line))) {
      out.preamble.push(line);
    } else {
      out.unparsed.push(line);
    }
    i++;
  }
  return out;
}

/** Total number of input lines accounted for — must equal the input length (the no-drop contract). */
export function accountedLines(p: ParsedPlan): number {
  const res = p.resources.reduce((n, r) => n + 1 + r.notes.length + r.body.length, 0);
  const summary = p.summary ? p.summary.split("\n").length : 0;
  return res + p.outputs.length + summary + p.preamble.length + p.unparsed.length;
}

export function groupByAction(p: ParsedPlan): { action: PlanAction; resources: PlanResource[] }[] {
  return ACTION_ORDER.map((action) => ({
    action,
    resources: p.resources.filter((r) => r.action === action && !r.tagOnly),
  })).filter((g) => g.resources.length > 0);
}
