import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { DOC_TOPICS, docsUrl } from "./docs";

// Every help link must land on a real heading of the user guide: renaming a heading in docs/guide/
// fails here, not in front of a user (DESIGN §7, Help).

const DOCS_DIR = resolve(__dirname, "../../../docs");

/** Python-Markdown's toc slugify (MkDocs' default): what `id` a heading gets. */
function slugify(heading: string): string {
  const text = heading
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_]{2}([^*_]+)[*_]{2}/g, "$1")
    .normalize("NFKD")
    .replace(/[\u0080-\uffff]/g, ""); // NFKD + ASCII only, like Python's encode("ascii", "ignore")
  return text
    .replace(/[^\w\s-]/g, "")
    .trim()
    .toLowerCase()
    .replace(/[-\s]+/g, "-");
}

function anchorsOf(markdown: string): Set<string> {
  const anchors = new Set<string>();
  let fenced = false;
  for (const line of markdown.split("\n")) {
    if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
    const m = !fenced && /^#{1,6}\s+(.+?)\s*#*\s*$/.exec(line);
    if (m) anchors.add(slugify(m[1]));
  }
  return anchors;
}

describe("help links (lib/docs)", () => {
  for (const [topic, target] of Object.entries(DOC_TOPICS) as [string, { page: string; anchor?: string }][]) {
    it(`${topic} → ${target.page}${target.anchor ? `#${target.anchor}` : ""} exists`, () => {
      const file = resolve(DOCS_DIR, `${target.page}.md`);
      expect(existsSync(file), `${file} is missing`).toBe(true);
      if (target.anchor) expect([...anchorsOf(readFileSync(file, "utf8"))]).toContain(target.anchor);
    });
  }

  it("slugifies headings the way MkDocs does", () => {
    expect(slugify("Who can approve — `can_apply`")).toBe("who-can-approve-can_apply");
    expect(slugify("Mocks & the apply block")).toBe("mocks-the-apply-block");
    expect(slugify("1. Configure the cloud provider (AWS)")).toBe("1-configure-the-cloud-provider-aws");
  });

  it("builds directory URLs under the guide's base", () => {
    expect(docsUrl("fourEyes")).toMatch(/guide\/runs-and-approvals\/#four-eyes$/);
    expect(docsUrl("guide")).toMatch(/\/$/);
  });
});
