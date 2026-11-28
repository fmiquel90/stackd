// Where the UI's help links point (DESIGN §7, Help): one registry, so a link never hard-codes a
// guide URL and a unit test (docs.test.ts) can check every target against the headings of
// docs/guide/. Only the user guide — never the decision documents.

/** The guide's root, same-origin: the front image serves the built guide under /docs/; in dev, Vite
 *  proxies /docs/ to mkdocs serve (the compose `docs` service). */
export const DOCS_BASE = "/docs/";

/** Topic → guide page (path under docs/, without `.md`) and optional heading anchor. */
export const DOC_TOPICS = {
  guide: { page: "index" },
  canApply: { page: "guide/runs-and-approvals", anchor: "who-can-approve-can_apply" },
  fourEyes: { page: "guide/runs-and-approvals", anchor: "four-eyes" },
  mocks: { page: "guide/dependencies-and-mocks", anchor: "mocks-the-apply-block" },
  secretFallback: { page: "guide/external-secrets", anchor: "fallback-blocks-apply-by-default" },
  envSettings: { page: "guide/stacks-and-environments", anchor: "fields-that-matter" },
  managedState: { page: "guide/importing-existing-state", anchor: "which-to-choose" },
  defineStack: { page: "guide/stacks-and-environments", anchor: "defining-a-stack" },
  runWorker: { page: "guide/workers-and-scaling", anchor: "run-a-worker" },
} as const satisfies Record<string, { page: string; anchor?: string }>;

export type DocTopic = keyof typeof DOC_TOPICS;

/** The URL of a topic: MkDocs directory URLs (`guide/x/`), the site root for `index`. */
export function docsUrl(topic: DocTopic): string {
  const t: { page: string; anchor?: string } = DOC_TOPICS[topic];
  const path = t.page === "index" ? "" : `${t.page}/`;
  return `${DOCS_BASE}${path}${t.anchor ? `#${t.anchor}` : ""}`;
}
