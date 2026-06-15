import { api } from "./client";
import type {
  AuditEvent,
  CommentAnchor,
  Environment,
  Health,
  LogChunk,
  LogEntry,
  RunComment,
  QueueEntry,
  RepoAuthKind,
  ResolvedVariable,
  Role,
  Run,
  Stack,
  Tier,
  TierDef,
  Tool,
  User,
  UserNotification,
  Variable,
  VariableKind,
  VariableSet,
} from "./types";

export interface UserUpdate {
  role?: Role;
  allowed_tiers?: string[];
  can_destroy?: boolean;
  disabled?: boolean;
}

// Admin-only (server enforces require_role(admin); the UI also hides it for non-admins).
export const users = {
  list: () => api<User[]>("/users"),
  update: (id: string, body: UserUpdate) => api<User>(`/users/${id}`, { method: "PATCH", body }),
};

// Configurable tier catalog (§2.4). Listing is open (forms need it); mutations are admin.
export interface NewTier {
  name: string;
  requires_four_eyes?: boolean;
  position?: number;
}

export const tiers = {
  list: () => api<TierDef[]>("/tiers"),
  create: (body: NewTier) => api<TierDef>("/tiers", { body }),
  update: (id: string, body: { requires_four_eyes?: boolean; position?: number }) =>
    api<TierDef>(`/tiers/${id}`, { method: "PATCH", body }),
  remove: (id: string) => api<void>(`/tiers/${id}`, { method: "DELETE" }),
};

export interface StackPatch {
  name?: string;
  description?: string | null;
  repo_url?: string;
  repo_auth_kind?: RepoAuthKind;
  repo_secret?: string; // write-only; "" clears it, omit to keep
  webhook_secret?: string; // write-only HMAC secret; "" clears it, omit to keep
  project_root?: string;
  tool?: Tool;
  tool_version?: string;
}

export interface CheckRepoResult {
  ok: boolean;
  branches: string[];
  detail: string | null;
}

export interface NewStack {
  name: string;
  repo_url: string;
  tool: Tool;
  tool_version: string;
  project_root?: string;
  description?: string;
}

export interface NewEnvironment {
  name: string;
  tier: Tier;
  branch: string;
  protected?: boolean;
  autodeploy?: boolean;
}

export const stacks = {
  list: () => api<Stack[]>("/stacks"),
  get: (id: string) => api<Stack>(`/stacks/${id}`),
  create: (body: NewStack) => api<Stack>("/stacks", { body }),
  update: (id: string, body: StackPatch) => api<Stack>(`/stacks/${id}`, { method: "PATCH", body }),
  remove: (id: string) => api<void>(`/stacks/${id}`, { method: "DELETE" }),
  checkRepo: (id: string) =>
    api<CheckRepoResult>(`/stacks/${id}/check-repo`, { method: "POST" }),
  environments: (id: string) => api<Environment[]>(`/stacks/${id}/environments`),
  createEnvironment: (id: string, body: NewEnvironment) =>
    api<Environment>(`/stacks/${id}/environments`, { body }),
  // Stack-level variables (environment_id NULL — common to every env of the stack).
  variables: (id: string) => api<Variable[]>(`/stacks/${id}/variables`),
  addVariable: (id: string, body: NewVariable) =>
    api<Variable>(`/stacks/${id}/variables`, { body }),
  removeVariable: (id: string, varId: string) =>
    api<void>(`/stacks/${id}/variables/${varId}`, { method: "DELETE" }),
};

export const environments = {
  get: (id: string) => api<Environment>(`/environments/${id}`),
  resolvedVariables: (id: string) =>
    api<ResolvedVariable[]>(`/environments/${id}/resolved-variables`),
};

export interface NewVariable {
  kind: VariableKind;
  name: string;
  value: string;
  sensitive?: boolean;
  hcl?: boolean;
}

export const variableSets = {
  list: () => api<VariableSet[]>("/variable-sets"),
  create: (body: { name: string; description?: string; auto_attach?: boolean }) =>
    api<VariableSet>("/variable-sets", { body }),
  remove: (id: string) => api<void>(`/variable-sets/${id}`, { method: "DELETE" }),
  variables: (setId: string) => api<Variable[]>(`/variable-sets/${setId}/variables`),
  addVariable: (setId: string, body: NewVariable) =>
    api<Variable>(`/variable-sets/${setId}/variables`, { body }),
  updateVariable: (setId: string, varId: string, body: { value?: string; sensitive?: boolean }) =>
    api<Variable>(`/variable-sets/${setId}/variables/${varId}`, { method: "PATCH", body }),
  removeVariable: (setId: string, varId: string) =>
    api<void>(`/variable-sets/${setId}/variables/${varId}`, { method: "DELETE" }),
};

export interface NewComment {
  body: string;
  anchor?: CommentAnchor | null;
  parent_id?: string | null;
}

export const comments = {
  list: (runId: string) => api<RunComment[]>(`/runs/${runId}/comments`),
  create: (runId: string, body: NewComment) =>
    api<RunComment>(`/runs/${runId}/comments`, { body }),
  update: (runId: string, cid: string, body: { body?: string; resolved?: boolean }) =>
    api<RunComment>(`/runs/${runId}/comments/${cid}`, { method: "PATCH", body }),
  remove: (runId: string, cid: string) =>
    api<void>(`/runs/${runId}/comments/${cid}`, { method: "DELETE" }),
};

export const runs = {
  list: (envId: string) => api<Run[]>(`/environments/${envId}/runs`),
  get: (id: string) => api<Run>(`/runs/${id}`),
  trigger: (envId: string, type: "tracked" | "destroy" = "tracked") =>
    api<Run>(`/environments/${envId}/runs`, { body: { type } }),
  confirm: (id: string) => api<Run>(`/runs/${id}/confirm`, { method: "POST" }),
  discard: (id: string) => api<Run>(`/runs/${id}/discard`, { method: "POST" }),
  cancel: (id: string) => api<Run>(`/runs/${id}/cancel`, { method: "POST" }),
  logs: (id: string) => api<LogChunk[]>(`/runs/${id}/logs`),
  command: (envId: string, command: string, args: string[]) =>
    api<Run>(`/environments/${envId}/commands`, { body: { command, args } }),
  promote: (targetEnvId: string, fromEnvId: string) =>
    api<Run>(`/environments/${targetEnvId}/promote`, { body: { from_environment_id: fromEnvId } }),
};

// Allowlisted ad-hoc subcommands (mirrors app/runs/commands.py). Mutating ones need apply rights.
export const COMMANDS_READONLY = ["output", "show", "state list", "state show", "validate", "providers"];
export const COMMANDS_MUTATING = ["import", "state rm", "state mv", "taint", "untaint", "refresh"];

export const queue = {
  list: () => api<QueueEntry[]>("/queue"),
};

// In-app notification center (§17) — the current user's inbox.
export const inbox = {
  list: () => api<UserNotification[]>("/notifications"),
  markRead: (ids?: string[]) =>
    api<void>("/notifications/read", { method: "POST", body: { ids: ids ?? null } }),
};

export const audit = {
  list: (params: Record<string, string> = {}) => {
    const q = new URLSearchParams(params).toString();
    return api<AuditEvent[]>(`/audit${q ? `?${q}` : ""}`);
  },
};

export const observability = {
  health: () => api<Health>("/health"),
  logs: (params: Record<string, string> = {}) => {
    const q = new URLSearchParams(params).toString();
    return api<{ total: number; items: LogEntry[] }>(`/logs${q ? `?${q}` : ""}`);
  },
};

export interface Diagnostics {
  status: "none" | "pending" | "sent" | "done" | "failed";
  result: Record<string, unknown> | null;
  requested_at?: string;
  completed_at?: string | null;
}

export const workers = {
  requestDiagnostics: (id: string) =>
    api<{ command_id: string }>(`/workers/${id}/diagnostics`, { method: "POST" }),
  diagnostics: (id: string) => api<Diagnostics>(`/workers/${id}/diagnostics`),
};

export type HookStage =
  | "before_init"
  | "after_init"
  | "before_plan"
  | "after_plan"
  | "before_apply"
  | "after_apply";

export interface Hook {
  id: string;
  target_kind: string;
  target_id: string;
  stage: HookStage;
  name: string;
  command: string;
  on_failure: "fail" | "warn";
  position: number;
}

export type HookScope = "stacks" | "environments";

export const hooksApi = {
  list: (scope: HookScope, id: string) => api<Hook[]>(`/${scope}/${id}/hooks`),
  create: (
    scope: HookScope,
    id: string,
    body: { stage: HookStage; name: string; command: string; on_failure: "fail" | "warn" },
  ) => api<Hook>(`/${scope}/${id}/hooks`, { body }),
  remove: (scope: HookScope, id: string, hookId: string) =>
    api<void>(`/${scope}/${id}/hooks/${hookId}`, { method: "DELETE" }),
};

export type NotificationKind = "slack" | "webhook";
export type NotificationState = "unconfirmed" | "finished" | "failed";

export interface NotificationTarget {
  id: string;
  target_kind: string;
  target_id: string;
  name: string;
  kind: NotificationKind;
  url: string;
  on_states: NotificationState[];
  enabled: boolean;
}

export interface NotificationCreate {
  name: string;
  kind: NotificationKind;
  url: string;
  on_states: NotificationState[];
}

// Reuses HookScope ("stacks" | "environments") — notifications attach exactly like platform hooks.
export const notificationsApi = {
  list: (scope: HookScope, id: string) =>
    api<NotificationTarget[]>(`/${scope}/${id}/notifications`),
  create: (scope: HookScope, id: string, body: NotificationCreate) =>
    api<NotificationTarget>(`/${scope}/${id}/notifications`, { body }),
  update: (scope: HookScope, id: string, targetId: string, body: Partial<NotificationCreate> & { enabled?: boolean }) =>
    api<NotificationTarget>(`/${scope}/${id}/notifications/${targetId}`, { method: "PATCH", body }),
  remove: (scope: HookScope, id: string, targetId: string) =>
    api<void>(`/${scope}/${id}/notifications/${targetId}`, { method: "DELETE" }),
};

export interface Dependency {
  id: string;
  upstream_env_id: string;
  trigger_policy: string;
  references: { output_name: string; input_name: string; has_mock: boolean }[];
}

export interface NewDependency {
  upstream_env_id: string;
  trigger_policy: string;
  references: { output_name: string; input_name: string; mock_value?: string | null }[];
}

export const dependenciesApi = {
  list: (envId: string) => api<Dependency[]>(`/environments/${envId}/dependencies`),
  create: (envId: string, body: NewDependency) =>
    api<{ id: string }>(`/environments/${envId}/dependencies`, { body }),
  remove: (depId: string) => api<void>(`/dependencies/${depId}`, { method: "DELETE" }),
};

export interface GraphNode {
  id: string;
  name: string;
  stack_id: string;
  tier: string;
}
export interface GraphEdge {
  id: string;
  upstream: string;
  downstream: string;
  policy: string;
  references: number;
  has_mock: boolean;
}

export const graphApi = {
  get: () => api<{ nodes: GraphNode[]; edges: GraphEdge[] }>("/graph"),
};

export interface StateVersion {
  id: string;
  serial: number;
  lineage: string | null;
  size_bytes: number;
  created_by_run_id: string | null;
  created_at: string;
}

export const stateApi = {
  versions: (envId: string) => api<StateVersion[]>(`/environments/${envId}/state/versions`),
  forceUnlock: (envId: string) =>
    api<void>(`/environments/${envId}/state/lock`, { method: "DELETE" }),
};

export interface CloudIntegration {
  id: string;
  provider: string;
  plan_role_arn: string;
  apply_role_arn: string;
  region: string | null;
  session_duration: number;
}

export const cloudApi = {
  get: (envId: string) => api<CloudIntegration>(`/environments/${envId}/cloud-integration`),
  put: (
    envId: string,
    body: { plan_role_arn: string; apply_role_arn: string; region?: string | null },
  ) => api<CloudIntegration>(`/environments/${envId}/cloud-integration`, { method: "PUT", body }),
  remove: (envId: string) =>
    api<void>(`/environments/${envId}/cloud-integration`, { method: "DELETE" }),
  test: (envId: string) =>
    api<{ assumed_role: string }>(`/environments/${envId}/cloud-integration/test`, { method: "POST" }),
};

// External secret sources (SPECS §15) — space-scoped connections to a secrets manager.
export type SecretProvider = "proton_pass";

export interface SecretSource {
  id: string;
  space_id: string;
  name: string;
  provider: SecretProvider;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface NewSecretSource {
  name: string;
  provider: SecretProvider;
  config?: Record<string, unknown>;
  bootstrap_secret: string; // write-only
}

export const secretSourcesApi = {
  list: (spaceId: string) => api<SecretSource[]>(`/spaces/${spaceId}/secret-sources`),
  create: (spaceId: string, body: NewSecretSource) =>
    api<SecretSource>(`/spaces/${spaceId}/secret-sources`, { body }),
  rotate: (spaceId: string, srcId: string, bootstrap_secret: string) =>
    api<SecretSource>(`/spaces/${spaceId}/secret-sources/${srcId}`, {
      method: "PATCH",
      body: { bootstrap_secret },
    }),
  remove: (spaceId: string, srcId: string) =>
    api<void>(`/spaces/${spaceId}/secret-sources/${srcId}`, { method: "DELETE" }),
};
