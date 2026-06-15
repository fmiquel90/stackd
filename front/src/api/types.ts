export type Role = "reader" | "writer" | "approver" | "admin";
// Tiers are a configurable catalog now (no fixed dev/staging/prod) — a tier is referenced by name.
export type Tier = string;

export interface TierDef {
  id: string;
  name: string;
  requires_four_eyes: boolean;
  position: number;
  created_at: string;
}

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  avatar_url: string | null;
  role: Role;
  allowed_tiers: string[];
  can_destroy: boolean;
  disabled: boolean;
  onboarded: boolean;
  last_login_at: string | null;
}

export interface Session {
  access_token: string;
  user: User;
}

export interface DevPersona {
  key: string;
  email: string;
  role: Role;
}

export type RepoAuthKind = "none" | "token" | "deploy_key";
export type Tool = "opentofu" | "terraform";
export type VariableKind = "terraform" | "environment";

export interface Stack {
  id: string;
  space_id: string;
  name: string;
  description: string | null;
  repo_url: string;
  repo_auth_kind: RepoAuthKind;
  has_repo_secret: boolean;
  has_webhook_secret: boolean;
  project_root: string;
  tool: Tool;
  tool_version: string;
  created_at: string;
  updated_at: string;
}

export interface Environment {
  id: string;
  stack_id: string;
  name: string;
  tier: Tier;
  branch: string;
  autodeploy: boolean;
  protected: boolean;
  require_second_pair_of_eyes: boolean;
  managed_state: boolean;
  allow_mock_apply: boolean;
  allow_fallback_apply: boolean;
  head_sha: string | null;
  commits_ahead: number | null;
  affects_project_root: boolean | null;
  stale: boolean;
  locked: boolean;
  labels: Record<string, unknown> | null;
  position: number;
}

export interface ResolvedVariable {
  name: string;
  injected_name: string;
  kind: VariableKind;
  sensitive: boolean;
  hcl: boolean;
  provenance: string;
  value: string | null;
}

export interface VariableSet {
  id: string;
  space_id: string;
  name: string;
  description: string | null;
  auto_attach: boolean;
}

export type SecretFallbackMode = "error" | "static" | "break_glass";

export interface Variable {
  id: string;
  kind: VariableKind;
  name: string;
  sensitive: boolean;
  hcl: boolean;
  value: string | null; // masked ("•••") for sensitive / reference variables
  secret_source_id: string | null;
  secret_ref: string | null;
  secret_fallback_mode: SecretFallbackMode | null;
}

export type RunState =
  | "queued"
  | "preparing"
  | "planning"
  | "checking"
  | "unconfirmed"
  | "confirmed"
  | "applying"
  | "finished"
  | "failed"
  | "discarded"
  | "canceled";

export interface Run {
  id: string;
  environment_id: string;
  type: "tracked" | "proposed" | "destroy";
  state: RunState;
  commit_sha: string | null;
  commit_message: string | null;
  triggered_by: "manual" | "webhook" | "dependency" | "api";
  trigger_user_id: string | null;
  confirmed_by_user_id: string | null;
  plan_summary: { add?: number; change?: number; destroy?: number } | null;
  check_results: { checks?: { name: string; status: string; detail?: string }[] } | null;
  used_mocks: boolean;
  used_secret_fallback: boolean;
  variable_provenance: Record<string, string> | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface LogChunk {
  phase: string;
  section: string | null;
  seq: number;
  lines: { t: string; msg: string }[];
}

export type NotificationKindInbox =
  | "approval_request"
  | "run_finished"
  | "run_failed"
  | "comment_reply"
  | "mention";

export interface UserNotification {
  id: string;
  kind: NotificationKindInbox;
  run_id: string | null;
  comment_id: string | null;
  context: Record<string, unknown> | null;
  read: boolean;
  created_at: string;
}

// Plan-review comment (SPECS §16). `anchor` is null for a general thread, or pins it to the plan.
export interface CommentAnchor {
  kind: "plan_line" | "resource";
  phase?: string;
  seq?: number;
  line_start?: number;
  line_end?: number;
  snippet?: string;
  address?: string;
  action?: string;
}

export interface RunComment {
  id: string;
  run_id: string;
  parent_id: string | null;
  author_user_id: string | null;
  author_email: string | null;
  body: string;
  anchor: CommentAnchor | null;
  resolved: boolean;
  resolved_by_user_id: string | null;
  created_at: string;
  edited_at: string | null;
}

export interface AuditEvent {
  id: string;
  actor_kind: string;
  actor_email: string | null;
  action: string;
  target_kind: string | null;
  target_id: string | null;
  context: Record<string, unknown> | null;
  created_at: string;
}

export interface QueueEntry {
  run_id: string;
  environment_id: string;
  state: string;
  worker_id: string | null;
  blocking_reason: string | null;
}

export interface HealthWorker {
  id: string;
  name: string;
  pool: string | null;
  pool_labels: Record<string, unknown> | null;
  labels: Record<string, unknown> | null;
  status: string;
  online: boolean;
  last_heartbeat_at: string | null;
  seconds_since_heartbeat: number | null;
  version: string | null;
}

export interface Health {
  status: string;
  env: string;
  version: string;
  checks: { database: string };
  workers: { total: number; online: number; items: HealthWorker[] };
  runs: { active: number; queued: number };
  log_buffer: { size: number; recent_warn_error: number };
}

export interface LogEntry {
  ts: string;
  level: string;
  logger: string;
  msg: string;
  event?: string;
  run_id?: string;
  worker_id?: string;
  request_id?: string;
  // http.request access logs carry these (see the API access-log middleware).
  method?: string;
  path?: string;
  status?: number;
  duration_ms?: number;
  [key: string]: unknown;
}
