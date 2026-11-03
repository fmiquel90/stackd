import type { AuditEvent, Environment, HealthWorker, Space, Stack, User } from "@/api/types";

// The audit journal's pure logic (DESIGN §5.6): action catalogue, row emphasis, target resolution
// and context summary. Context values are shown as recorded, except keys that could carry a secret.

/** Audit actions the API records (SPECS §6.3), grouped by domain for the action filter. Actions seen
 *  in loaded events are merged in, so a new one is never unfilterable. */
export const KNOWN_ACTIONS = [
  "auth.login",
  "auth.logout",
  "auth.domain_denied",
  "auth.refresh_reuse_detected",
  "run.triggered",
  "run.destroy_triggered",
  "run.command_triggered",
  "run.command_executed",
  "run.plan_finished",
  "run.check_warned",
  "run.check_failed",
  "run.confirmed",
  "run.discarded",
  "run.canceled",
  "run.applied",
  "run.apply_failed",
  "run.promoted",
  "dependency.created",
  "dependency.deleted",
  "dependency.mock_consumed",
  "secret.fallback_used",
  "secret.fallback_overridden",
  "space.created",
  "space.member_set",
  "space.member_removed",
  "stack.created",
  "stack.updated",
  "stack.deleted",
  "environment.created",
  "environment.updated",
  "environment.deleted",
  "environment.head_refreshed",
  "environment.inputs_discovered",
  "environment.drift_detected",
  "environment.drift_cleared",
  "state.force_unlocked",
  "state.import_session_created",
  "variable.created",
  "variable.updated",
  "variable.deleted",
  "variable_set.created",
  "variable_set.updated",
  "variable_set.deleted",
  "variable_set.attached",
  "variable_set.detached",
  "secret_source.created",
  "secret_source.updated",
  "secret_source.deleted",
  "secret_source.token_rotated",
  "cloud_integration.created",
  "cloud_integration.updated",
  "cloud_integration.deleted",
  "hook.created",
  "hook.updated",
  "hook.deleted",
  "notification_target.created",
  "notification_target.updated",
  "notification_target.deleted",
  "tier.created",
  "tier.updated",
  "tier.deleted",
  "user.role_changed",
  "user.apply_tier_changed",
  "user.destroy_permission_changed",
  "user.disabled",
  "worker_pool.created",
  "worker_pool.deleted",
  "worker.diagnostics_requested",
];

/** `{ domain: actions[] }`, domains sorted, for an `<optgroup>` select. */
export function actionGroups(seen: string[]): [string, string[]][] {
  const all = [...new Set([...KNOWN_ACTIONS, ...seen])];
  const map = new Map<string, string[]>();
  for (const a of all) {
    const domain = a.split(".")[0];
    map.set(domain, [...(map.get(domain) ?? []), a]);
  }
  return [...map.entries()]
    .map(([d, as]) => [d, as.sort()] as [string, string[]])
    .sort((a, b) => a[0].localeCompare(b[0]));
}

/** Sign-ins are ~90 % of the volume: hidden unless asked for. */
export function isSignIn(action: string): boolean {
  return action === "auth.login" || action === "auth.refresh" || action === "auth.logout";
}

/** Rows that changed real infrastructure, or authorised it, stand out (a label carries it too). */
export function isConsequential(action: string): boolean {
  return action === "run.confirmed" || action === "run.applied" || action.includes("destroy");
}

export interface Lookup {
  stacks: Stack[];
  envs: Environment[];
  spaces: Space[];
  workers: HealthWorker[];
  users: User[];
}

export interface Target {
  label: string;
  to?: string;
  /** The entity no longer exists: the label comes from the event's own context. */
  gone?: boolean;
}

const short = (id: string) => id.slice(0, 8);
const str = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined);

export function envPath(env: Environment, l: Lookup): { label: string; to?: string } {
  const stack = l.stacks.find((s) => s.id === env.stack_id);
  if (!stack) return { label: env.name };
  const space = l.spaces.find((s) => s.id === stack.space_id);
  const label = `${stack.name}/${env.name}`;
  if (!space) return { label };
  return {
    label,
    to: `/s/${encodeURIComponent(space.name)}/stacks/${encodeURIComponent(stack.name)}/envs/${encodeURIComponent(env.name)}`,
  };
}

export function targetOf(e: AuditEvent, l: Lookup): Target | null {
  const ctx = e.context ?? {};
  const id = e.target_id;
  if (!e.target_kind || !id) return null;
  const name = str(ctx.name);
  switch (e.target_kind) {
    case "run": {
      const envId = str(ctx.environment_id);
      const env = envId ? l.envs.find((x) => x.id === envId) : undefined;
      return { label: `run ${short(id)}${env ? ` · ${envPath(env, l).label}` : ""}`, to: `/runs/${id}` };
    }
    case "environment": {
      const env = l.envs.find((x) => x.id === id);
      if (env) return envPath(env, l);
      return { label: `environment ${name ?? short(id)}`, gone: true };
    }
    case "stack": {
      const stack = l.stacks.find((s) => s.id === id);
      const space = stack && l.spaces.find((s) => s.id === stack.space_id);
      if (stack && space)
        return {
          label: stack.name,
          to: `/s/${encodeURIComponent(space.name)}/stacks/${encodeURIComponent(stack.name)}`,
        };
      return { label: `stack ${name ?? short(id)}`, gone: true };
    }
    case "worker": {
      const w = l.workers.find((x) => x.id === id);
      return w
        ? { label: `worker ${w.name}`, to: `/workers?worker=${encodeURIComponent(w.name)}` }
        : { label: `worker ${short(id)}`, gone: true };
    }
    case "worker_pool":
      return { label: `pool ${name ?? short(id)}`, to: "/workers" };
    case "user": {
      const u = l.users.find((x) => x.id === id);
      return { label: u ? u.email : `user ${short(id)}` };
    }
    default:
      return { label: `${e.target_kind.replace(/_/g, " ")} ${name ?? short(id)}` };
  }
}

/** Keys never displayed: they could hold a credential, or only repeat the target. */
const HIDDEN =
  /value|secret|token|password|private|user_agent|^environment_id$|^target_id$|^upstream_env_id$/i;

/** The context in a few words: `planning → finished`, `tier prod`, `commit 2af0351`… */
export function contextSummary(e: AuditEvent, target: Target | null): string[] {
  const ctx = e.context ?? {};
  const out: string[] = [];
  const seen = new Set<string>();
  const take = (k: string) => {
    seen.add(k);
    return ctx[k];
  };
  if (typeof ctx.from === "string" && typeof ctx.to === "string") out.push(`${take("from")} → ${take("to")}`);
  if (typeof ctx.has_changes === "boolean") out.push(take("has_changes") ? "has changes" : "no changes");
  if (typeof ctx.command === "string") {
    const args = Array.isArray(ctx.args) ? (take("args") as unknown[]).join(" ") : "";
    out.push(`${take("command")}${args ? ` ${args}` : ""}`.trim());
  }
  if (typeof ctx.commit === "string") out.push(`commit ${String(take("commit")).slice(0, 7)}`);
  if (typeof ctx.name === "string" && target?.label.includes(ctx.name)) seen.add("name");
  for (const [k, v] of Object.entries(ctx)) {
    if (seen.has(k) || HIDDEN.test(k) || v === null || v === undefined) continue;
    if (Array.isArray(v)) out.push(`${k.replace(/_/g, " ")} ${v.map(String).join(", ")}`);
    else if (typeof v === "boolean") out.push(v ? k.replace(/_/g, " ") : `not ${k.replace(/_/g, " ")}`);
    else if (typeof v !== "object") out.push(`${k.replace(/_/g, " ")} ${String(v)}`);
  }
  return out;
}

/** URL filters → API query (`from_`/`to` are ISO instants; a day filter spans the whole local day). */
export interface AuditFilters {
  actor: string;
  action: string;
  kind: string;
  target: string;
  from: string;
  to: string;
}

export function apiParams(f: AuditFilters, cursor?: string): Record<string, string> {
  const p: Record<string, string> = {};
  if (f.actor) p.actor = f.actor;
  if (f.action) p.action = f.action;
  if (f.kind) p.target_kind = f.kind;
  if (f.target) p.target_id = f.target;
  if (f.from) p.from_ = new Date(`${f.from}T00:00:00`).toISOString();
  if (cursor) p.to = cursor;
  else if (f.to) p.to = new Date(`${f.to}T23:59:59.999`).toISOString();
  return p;
}
