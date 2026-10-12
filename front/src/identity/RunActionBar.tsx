import { useId, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ChevronLeft, ChevronRight } from "lucide-react";
import type { Environment, Run } from "@/api/types";
import type { RunRights } from "@/lib/permissions";
import { formatCounts, outcomeOf, planCounts } from "@/lib/runOutcome";
import { relative } from "@/lib/time";
import { Button } from "@/ui/Button";
import { Dialog, DialogTrigger, Popover, PopoverContent, PopoverTrigger, SheetContent } from "@/ui/overlays";
import { Banner, Kbd } from "@/ui/primitives";
import { MOBILE, useMediaQuery } from "@/ui/useMediaQuery";
import { cn } from "@/ui/cn";
import { ModifierChips, RunStateBadge } from "./StateBadge";
import { TypeToConfirm, type CascadeItem } from "./TypeToConfirm";

// The decision bar (DESIGN §5.2). It says what the run waits for or how it ended, who triggered and
// confirmed it, where the viewer stands, what the apply can trigger downstream — and every action
// that applies to the state, with the reason in visible text when the viewer cannot take it.

export interface RunActionBarProps {
  run: Run;
  env: Environment;
  rights: RunRights;
  /** Display names resolved from user ids (GET /users/mentionable). */
  triggerer?: string | null;
  confirmer?: string | null;
  me: { email: string; role: string; allowed_tiers: string[] };
  /** "an approver with tier prod other than bob@…" (permissions.whoMayConfirm). */
  whoMayConfirm: string | null;
  cascade: CascadeItem[];
  destroyed: string[];
  replaced: string[];
  /** Permissions may differ per space and are not readable client-side (AUDIT A2). */
  instanceCaveat?: boolean;
  pending?: Partial<Record<"confirm" | "discard" | "cancel" | "replan", boolean>>;
  /** The API's own refusal, shown verbatim. */
  apiError?: string | null;
  onConfirm: () => void;
  onDiscard: () => void;
  onCancel: () => void;
  onReplan: () => void;
  /** Next/previous run that needs the viewer (DESIGN §5.2 next decision). */
  prevDecision?: string | null;
  nextDecision?: string | null;
  className?: string;
}

const RUNNING_WORDS: Partial<Record<Run["state"], string>> = {
  preparing: "Preparing the workspace…",
  planning: "Planning…",
  checking: "Running checks…",
  confirmed: "Apply confirmed — waiting for a worker…",
  applying: "Applying…",
  running: "Running the command…",
};

export function stateSentence(
  run: Run,
  rights: RunRights,
  who: string | null,
  confirmer?: string | null,
): string {
  const outcome = outcomeOf(run);
  const counts = planCounts(run);
  switch (outcome) {
    case "applied":
      return `Applied ${counts ? formatCounts(counts) : ""} ${run.finished_at ? relative(run.finished_at) : ""} — ${
        run.confirmed_by_user_id ? `confirmed by ${confirmer ?? "a user"}` : "confirmed automatically"
      }.`.replace(/\s+/g, " ");
    case "no_changes":
      return "No changes — nothing was applied.";
    case "destroyed":
      return `Destroyed ${counts?.destroy ?? 0} resource(s) — confirmed by ${confirmer ?? "a user"}.`;
    case "planned":
      return "Plan-only run (pull request) — nothing is applied.";
    case "executed":
      return `Command ${run.command?.name ?? ""} executed.`;
    case "failed_during_apply":
      return "Failed during apply — infrastructure may be partially changed.";
    case "failed":
      return "Failed before anything was applied.";
    case "discarded":
      return "Discarded — nothing was applied.";
    case "canceled":
      return "Canceled — nothing was applied.";
  }
  if (run.state === "unconfirmed") {
    if (rights.blockedForAll.length) return `Apply is blocked: ${rights.blockedForAll[0]}.`;
    if (rights.needsYou) return "Waiting for your decision.";
    return `Waiting for a decision — needs ${who ?? "an authorized approver"}.`;
  }
  if (run.state === "queued") return "Queued — waiting for the environment and a worker.";
  return RUNNING_WORDS[run.state] ?? run.state;
}

function Reason({ id, action, reasons }: { id: string; action: string; reasons: string[] }) {
  return (
    <li id={id}>
      <span className="font-semibold">{action} unavailable:</span> {reasons.join("; ")}.
    </li>
  );
}

export function RunActionBar(props: RunActionBarProps) {
  const {
    run,
    env,
    rights,
    triggerer,
    confirmer,
    me,
    whoMayConfirm,
    cascade,
    pending = {},
    apiError,
  } = props;
  const [open, setOpen] = useState(false);
  const mobile = useMediaQuery(MOBILE);
  const rid = useId();
  const counts = planCounts(run);
  const pendingRun = run.state === "unconfirmed";

  const confirmBtn = (
    <Button
      data-action="confirm"
      aria-keyshortcuts="c"
      variant="decision"
      size={mobile ? "touch" : "md"}
      className={mobile ? "flex-1" : undefined}
      disabled={rights.confirm.reasons.length > 0}
      aria-describedby={rights.confirm.reasons.length ? `${rid}-confirm` : undefined}
      loading={pending.confirm}
      onClick={rights.highRisk ? undefined : props.onConfirm}
    >
      {run.type === "destroy" ? "Confirm destroy" : "Confirm apply"}
      {!mobile && <Kbd>c</Kbd>}
    </Button>
  );

  const friction = (
    <TypeToConfirm
      name={env.name}
      verb={run.type === "destroy" ? "destroy" : "apply"}
      because={[...(run.type === "destroy" ? ["destroy run"] : []), ...rights.fourEyes.because]}
      counts={counts}
      destroyed={props.destroyed}
      replaced={props.replaced}
      cascade={cascade}
      pending={pending.confirm}
      onCancel={() => setOpen(false)}
      onConfirm={() => {
        props.onConfirm();
      }}
    />
  );

  let confirmControl: ReactNode = null;
  if (rights.confirm.visible) {
    if (!rights.highRisk || rights.confirm.reasons.length) confirmControl = confirmBtn;
    else if (mobile)
      confirmControl = (
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>{confirmBtn}</DialogTrigger>
          <SheetContent side="bottom" title="Confirm">
            {friction}
          </SheetContent>
        </Dialog>
      );
    else
      confirmControl = (
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>{confirmBtn}</PopoverTrigger>
          <PopoverContent
            align="end"
            aria-label={`Confirm ${run.type === "destroy" ? "destroy" : "apply"} on ${env.name}`}
          >
            {friction}
          </PopoverContent>
        </Popover>
      );
  }

  const unavailable = (
    [
      ["Confirm", rights.confirm, "confirm"],
      ["Discard", rights.discard, "discard"],
      ["Cancel", rights.cancel, "cancel"],
    ] as const
  ).filter(([, r]) => r.visible && r.reasons.length);

  return (
    <section
      aria-label="Decision"
      className={cn(
        "flex flex-col gap-2 border-b border-line px-4 py-3",
        pendingRun && rights.needsYou ? "bg-tint-decision" : "bg-raised",
        props.className,
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2" aria-live="polite">
            <RunStateBadge
              run={run}
              decision={pendingRun ? (rights.needsYou ? "needsYou" : "waiting") : undefined}
            />
            <ModifierChips modifiers={{ mocked: run.used_mocks, fallback: run.used_secret_fallback }} />
            <span className="font-semibold">{stateSentence(run, rights, whoMayConfirm, confirmer)}</span>
          </div>
          {rights.fourEyes.required && run.type !== "proposed" && (
            <div className="text-[12px]">
              <span className="eyebrow mr-1 text-muted">Four-eyes</span>({rights.fourEyes.because.join(", ")}
              ):{" "}
              {run.confirmed_by_user_id
                ? `triggered by ${triggerer ?? (rights.fourEyes.triggererId ? "a user" : run.triggered_by)} · confirmed by ${confirmer ?? "a user"}.`
                : rights.fourEyes.triggererId
                  ? `${triggerer ?? "a user"} triggered this run — someone else must confirm it.`
                  : `triggered by ${run.triggered_by} — any authorized approver may confirm.`}
            </div>
          )}
          {!rights.fourEyes.required && (triggerer || run.confirmed_by_user_id) && (
            <div className="text-[12px] text-muted">
              Triggered by {triggerer ?? run.triggered_by}
              {run.confirmed_by_user_id ? ` · confirmed by ${confirmer ?? "a user"}` : ""}
            </div>
          )}
          {pendingRun && (
            <div className="text-[12px]">
              You ({me.email} · {me.role} · {me.allowed_tiers.join(" ") || "no tiers"}){" "}
              {rights.confirm.reasons.length ? "cannot confirm." : "can confirm."}
              {props.instanceCaveat && (
                <span className="text-muted"> Based on your instance permissions.</span>
              )}
            </div>
          )}
          {pendingRun && cascade.length > 0 && (
            <div className="text-[12px]">
              <span className="eyebrow mr-1 text-muted">Can trigger</span>
              {cascade.map((c, i) => (
                <span key={c.label} className="font-data">
                  {i > 0 && ", "}
                  {c.label}
                  <span className="text-muted">
                    {c.likelihood === "will_plan"
                      ? " (will plan)"
                      : ` (if ${c.outputs?.join(", ") || "outputs"} change)`}
                  </span>
                </span>
              ))}
            </div>
          )}
        </div>

        <div
          className={cn(
            "flex items-center gap-2",
            mobile &&
              "fixed inset-x-0 bottom-0 z-30 border-t border-line bg-raised p-3 pb-[max(12px,env(safe-area-inset-bottom))]",
          )}
        >
          {rights.cancel.visible && (
            <Button
              data-action="cancel"
              aria-keyshortcuts="x"
              variant="ghost"
              size={mobile ? "touch" : "md"}
              disabled={rights.cancel.reasons.length > 0}
              aria-describedby={rights.cancel.reasons.length ? `${rid}-cancel` : undefined}
              loading={pending.cancel}
              onClick={props.onCancel}
            >
              Cancel run {!mobile && <Kbd>x</Kbd>}
            </Button>
          )}
          {rights.discard.visible && (
            <Button
              data-action="discard"
              aria-keyshortcuts="d"
              variant="secondary"
              size={mobile ? "touch" : "md"}
              disabled={rights.discard.reasons.length > 0}
              aria-describedby={rights.discard.reasons.length ? `${rid}-discard` : undefined}
              loading={pending.discard}
              onClick={props.onDiscard}
            >
              Discard {!mobile && <Kbd>d</Kbd>}
            </Button>
          )}
          {confirmControl}
          {!pendingRun && rights.replan.visible && !mobile && (
            <Button
              variant="secondary"
              disabled={rights.replan.reasons.length > 0}
              loading={pending.replan}
              onClick={props.onReplan}
            >
              Plan again
            </Button>
          )}
          {(props.prevDecision || props.nextDecision) && !mobile && (
            <span className="ml-2 flex items-center gap-1 border-l border-line pl-2">
              <DecisionLink
                to={props.prevDecision}
                label="Previous run that needs you"
                icon={<ChevronLeft size={14} aria-hidden />}
              />
              <span className="eyebrow text-muted">needs you</span>
              <DecisionLink
                to={props.nextDecision}
                label="Next run that needs you"
                icon={<ChevronRight size={14} aria-hidden />}
              />
            </span>
          )}
        </div>
      </div>

      {(unavailable.length > 0 ||
        (rights.cancel.visible === false &&
          ["preparing", "planning", "checking", "applying", "confirmed", "running"].includes(run.state))) && (
        <ul className="text-[12px] text-muted">
          {unavailable.map(([action, r, key]) => (
            <Reason key={key} id={`${rid}-${key}`} action={action} reasons={r.reasons} />
          ))}
          {!rights.cancel.visible &&
            ["preparing", "planning", "checking", "applying", "confirmed", "running"].includes(run.state) && (
              <li>Cancelling a run a worker is executing is not supported yet.</li>
            )}
        </ul>
      )}

      {apiError && (
        <Banner tone="failed" role="alert" title="The API refused the action">
          {apiError}
        </Banner>
      )}
    </section>
  );
}

function DecisionLink({ to, label, icon }: { to?: string | null; label: string; icon: ReactNode }) {
  if (!to)
    return (
      <span aria-hidden className="p-1 text-muted">
        {icon}
      </span>
    );
  return (
    <Link to={to} aria-label={label} title={label} className="rounded-base p-1 hover:bg-surface">
      {icon}
    </Link>
  );
}
