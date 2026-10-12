import type { Environment, Run, TierDef, User } from "@/api/types";

// Test and story factories: realistic defaults (the seeded demo), overridable per case.

let n = 0;
const id = (p: string) => `${p}-${(++n).toString().padStart(4, "0")}`;

export function makeRun(over: Partial<Run> = {}): Run {
  return {
    id: id("run"),
    environment_id: "env-prod",
    type: "tracked",
    state: "unconfirmed",
    command: null,
    commit_sha: "2af0351c9e",
    commit_message: "widen subnet, drop NAT",
    commit_author: "jdoe",
    triggered_by: "manual",
    trigger_user_id: "user-bob",
    confirmed_by_user_id: null,
    worker_id: "worker-vm",
    plan_summary: { add: 1, change: 1, destroy: 1 },
    check_results: null,
    used_mocks: false,
    used_secret_fallback: false,
    variable_provenance: null,
    error: null,
    claimed_at: "2026-10-10T20:00:02Z",
    confirmed_at: null,
    finished_at: null,
    created_at: "2026-10-10T20:00:00Z",
    ...over,
  };
}

export function makeEnv(over: Partial<Environment> = {}): Environment {
  return {
    id: "env-prod",
    stack_id: "stack-network",
    name: "prod",
    tier: "prod",
    branch: "main",
    autodeploy: false,
    protected: false,
    require_second_pair_of_eyes: false,
    managed_state: true,
    allow_mock_apply: false,
    allow_fallback_apply: false,
    backend_config_file: null,
    backend_config: null,
    head_sha: "2af0351c9e",
    commits_ahead: null,
    affects_project_root: null,
    stale: false,
    drift_status: "unknown",
    last_drift_checked_at: null,
    drift_run_id: null,
    drift_check_enabled: true,
    locked: false,
    labels: null,
    position: 2,
    ...over,
  };
}

export const TIERS: TierDef[] = [
  { id: "t-dev", name: "dev", requires_four_eyes: false, position: 0, created_at: "2026-01-01T00:00:00Z" },
  {
    id: "t-staging",
    name: "staging",
    requires_four_eyes: false,
    position: 1,
    created_at: "2026-01-01T00:00:00Z",
  },
  { id: "t-prod", name: "prod", requires_four_eyes: true, position: 2, created_at: "2026-01-01T00:00:00Z" },
];

export function makeUser(over: Partial<User> = {}): User {
  return {
    id: "user-alice",
    email: "alice@dev.local",
    display_name: null,
    avatar_url: null,
    role: "approver",
    allowed_tiers: ["dev", "staging", "prod"],
    can_destroy: false,
    disabled: false,
    onboarded: true,
    last_login_at: null,
    ...over,
  };
}

export const BOB = makeUser({
  id: "user-bob",
  email: "bob@dev.local",
  role: "writer",
  allowed_tiers: ["dev", "staging"],
});
export const ALICE = makeUser();
export const ADMIN = makeUser({
  id: "user-admin",
  email: "admin@dev.local",
  role: "admin",
  can_destroy: true,
});
