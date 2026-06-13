import { api } from "./client";
import type {
  AuditEvent,
  Environment,
  Health,
  LogChunk,
  LogEntry,
  QueueEntry,
  ResolvedVariable,
  Run,
  Stack,
  Tier,
  Tool,
  VariableSet,
} from "./types";

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
  environments: (id: string) => api<Environment[]>(`/stacks/${id}/environments`),
  createEnvironment: (id: string, body: NewEnvironment) =>
    api<Environment>(`/stacks/${id}/environments`, { body }),
};

export const environments = {
  get: (id: string) => api<Environment>(`/environments/${id}`),
  resolvedVariables: (id: string) =>
    api<ResolvedVariable[]>(`/environments/${id}/resolved-variables`),
};

export const variableSets = {
  list: () => api<VariableSet[]>("/variable-sets"),
  create: (body: { name: string; description?: string; auto_attach?: boolean }) =>
    api<VariableSet>("/variable-sets", { body }),
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
};

export const queue = {
  list: () => api<QueueEntry[]>("/queue"),
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
