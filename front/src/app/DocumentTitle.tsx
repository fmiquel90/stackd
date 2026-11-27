import { useEffect } from "react";
import { useLocation, useMatch } from "react-router-dom";
import type { Run, User } from "@/api/types";
import { useSpaceEnvs } from "@/features/data/spaceEnvs";
import { shortId } from "@/lib/ids";
import { outcomeOf } from "@/lib/runOutcome";
import { useRunIdentity } from "./runIdentity";
import { useSpaces } from "./SpaceContext";

// The browser tab title (DESIGN §4.1): the most specific name first, so a row of pinned tabs stays
// readable, and the "needs you" count up front, so a background tab tells its owner a decision is
// waiting. Same vocabulary as the screen: run short ID, lowercase state/outcome words.

const BRAND = "StackD";

export const PAGE_TITLES: Record<string, string> = {
  graph: "Graph",
  rollouts: "Rollouts",
  "variable-sets": "Variable sets",
  queue: "Queue",
  workers: "Workers",
  audit: "Audit",
  settings: "Settings",
};

/** `unconfirmed`, `applied`, `no changes`, `failed during apply`… — the badge's words, lowercased. */
export function runStatusWord(
  run: Pick<Run, "state" | "type" | "confirmed_at"> & Partial<Pick<Run, "error">>,
): string {
  return (outcomeOf(run) ?? run.state).replace(/_/g, " ");
}

export function composeTitle({
  subject,
  context,
  needsYou = 0,
}: {
  subject?: string | null;
  context?: string | null;
  needsYou?: number;
}): string {
  const head = [subject, context].filter(Boolean).join(" · ");
  const count = needsYou > 0 ? `(${needsYou}) ` : "";
  return `${count}${head ? `${head} — ` : ""}${BRAND}`;
}

/** Subject and context for a non-run route, from the URL alone (names are in the URL, DESIGN §4.1). */
export function routeTitle(pathname: string): { subject?: string; context?: string } {
  const parts = pathname.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "s" && parts[1]) {
    const [, space, section, name, sub, envName] = parts;
    if (!section) return { subject: "Overview", context: space };
    if (section === "stacks" && name && sub === "envs" && envName)
      return { subject: `${name}/${envName}`, context: space };
    if (section === "stacks" && name) return { subject: name, context: space };
    if (section === "variable-sets" && name) return { subject: name, context: "Variable sets" };
    return { subject: PAGE_TITLES[section] ?? section, context: space };
  }
  if (parts[0] === "rollouts" && parts[1]) return { subject: "Rollout", context: shortId(parts[1]) };
  if (parts[0]) return { subject: PAGE_TITLES[parts[0]] ?? parts[0] };
  return {};
}

function useNeedsYouCount(user: User): number {
  const { current } = useSpaces();
  return useSpaceEnvs(current?.id, user).needsYou.length;
}

function RunTitle({ runId, needsYou }: { runId: string; needsYou: number }) {
  const { run, env, stack } = useRunIdentity(runId);
  const where = stack && env ? `${stack.name}/${env.name} · ` : "";
  const subject = `${where}run ${shortId(runId)}`;
  const title = composeTitle({ subject, context: run ? runStatusWord(run) : null, needsYou });
  useEffect(() => {
    document.title = title;
  }, [title]);
  return null;
}

function RouteTitle({ pathname, needsYou }: { pathname: string; needsYou: number }) {
  const title = composeTitle({ ...routeTitle(pathname), needsYou });
  useEffect(() => {
    document.title = title;
  }, [title]);
  return null;
}

/** Renders nothing; keeps `document.title` in step with the route and live data. */
export function DocumentTitle({ user }: { user: User }) {
  const { pathname } = useLocation();
  const runMatch = useMatch("/runs/:runId");
  const needsYou = useNeedsYouCount(user);
  const runId = runMatch?.params.runId;
  return runId ? (
    <RunTitle runId={runId} needsYou={needsYou} />
  ) : (
    <RouteTitle pathname={pathname} needsYou={needsYou} />
  );
}
