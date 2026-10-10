import type { Environment, Role, Run, TierDef, User } from "@/api/types";

// What the viewer may do on a run, and why not (DESIGN §5.2). Mirrors the API's gates in order:
// can_apply (role, tier, destroy) → mock gate → fallback gate → four-eyes (api/app/runs/service.py,
// api/app/permissions.py). Computed from the *instance* permissions of /auth/me: the effective
// per-space membership is not readable by non-admins (AUDIT A2), so a refusal by the API is always
// shown verbatim on top of this.

const APPROVERS: Role[] = ["approver", "admin"];
const WRITERS: Role[] = ["writer", "approver", "admin"];

export interface ActionRight {
  /** The action applies to the run's current state (otherwise it is not rendered). */
  visible: boolean;
  /** Reasons the viewer cannot do it; empty = allowed. */
  reasons: string[];
}

export interface FourEyes {
  /** Four-eyes applies to this env. */
  required: boolean;
  /** What makes it apply: the tier flag (fail-closed when the tier is missing), protected, opt-in. */
  because: string[];
  /** The run's human triggerer, who therefore cannot confirm (null: no human at its origin). */
  triggererId: string | null;
}

export interface RunRights {
  confirm: ActionRight;
  discard: ActionRight;
  cancel: ActionRight;
  replan: ActionRight;
  fourEyes: FourEyes;
  /** Gates that block *everyone* (mocks / fallback not allowed on this env). */
  blockedForAll: string[];
  /** Typed-name friction: four-eyes env or any destroy (DESIGN §5.2). */
  highRisk: boolean;
  /** "Needs you": unconfirmed and the viewer may confirm (discarding is not enough). */
  needsYou: boolean;
}

export function fourEyesOf(
  run: Pick<Run, "triggered_by" | "trigger_user_id">,
  env: Environment,
  tier: TierDef | undefined,
): FourEyes {
  const because: string[] = [];
  if (!tier) because.push(`tier ${env.tier} is missing from the catalog (four-eyes enforced)`);
  else if (tier.requires_four_eyes) because.push(`tier ${env.tier}`);
  if (env.protected) because.push("protected environment");
  if (env.require_second_pair_of_eyes) because.push("environment requires a second pair of eyes");
  const human = run.triggered_by === "manual" ? run.trigger_user_id : null;
  return { required: because.length > 0, because, triggererId: human };
}

export function runRights(run: Run, env: Environment, tier: TierDef | undefined, me: User): RunRights {
  const fourEyes = fourEyesOf(run, env, tier);
  const unconfirmed = run.state === "unconfirmed";

  const blockedForAll: string[] = [];
  if (run.used_mocks && !env.allow_mock_apply)
    blockedForAll.push("mocked inputs — apply is disabled on this environment (allow_mock_apply is off)");
  if (run.used_secret_fallback && !env.allow_fallback_apply)
    blockedForAll.push("a secret came from a fallback — apply is disabled (allow_fallback_apply is off)");

  const confirmReasons: string[] = [];
  if (me.disabled) confirmReasons.push("your account is disabled");
  if (!APPROVERS.includes(me.role)) confirmReasons.push("approver role required");
  else if (me.allowed_tiers.length === 0)
    confirmReasons.push("no allowed tiers — you cannot confirm applies");
  else if (!me.allowed_tiers.includes(env.tier))
    confirmReasons.push(
      `tier ${env.tier} is not in your allowed tiers (${[...me.allowed_tiers].sort().join(", ")})`,
    );
  if (run.type === "destroy" && !me.can_destroy) confirmReasons.push("destroy permission required");
  confirmReasons.push(...blockedForAll);
  if (fourEyes.required && fourEyes.triggererId === me.id)
    confirmReasons.push("you triggered this run; four-eyes requires someone else to confirm");

  const writerReasons = me.disabled
    ? ["your account is disabled"]
    : WRITERS.includes(me.role)
      ? []
      : ["writer role required"];

  const confirm = { visible: unconfirmed, reasons: confirmReasons };
  return {
    confirm,
    discard: { visible: unconfirmed, reasons: writerReasons },
    cancel: { visible: run.state === "queued" || unconfirmed, reasons: writerReasons },
    replan: {
      visible: unconfirmed || ["finished", "failed", "discarded", "canceled"].includes(run.state),
      reasons: writerReasons,
    },
    fourEyes,
    blockedForAll,
    highRisk: run.type === "destroy" || fourEyes.required,
    needsYou: unconfirmed && confirmReasons.length === 0,
  };
}

/**
 * Who may still confirm, stated as a rule (names need AUDIT A3): "an approver with tier prod, other
 * than bob@dev.local". Null when nobody can (a gate blocks everyone) or the run is not waiting.
 */
export function whoMayConfirm(
  run: Run,
  env: Environment,
  rights: RunRights,
  triggererEmail?: string,
): string | null {
  if (run.state !== "unconfirmed" || rights.blockedForAll.length) return null;
  const parts = [`an approver with tier ${env.tier}`];
  if (run.type === "destroy") parts.push("and the destroy permission");
  if (rights.fourEyes.required && rights.fourEyes.triggererId)
    parts.push(`other than ${triggererEmail ?? "the person who triggered it"}`);
  return parts.join(" ");
}
