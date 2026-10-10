import { useId, useState, type FormEvent } from "react";
import type { PlanCounts } from "@/lib/runOutcome";
import { Button } from "@/ui/Button";
import { Input } from "@/ui/primitives";
import { PlanCountsBig } from "./PlanDiff";

// The typed-name friction (DESIGN §5.2, §5.7): a misclick safeguard, not a permission check — the API
// still evaluates can_apply. Destroyed and replaced addresses are listed in full, never truncated.

export interface CascadeItem {
  label: string;
  likelihood: "will_plan" | "plans_if_outputs_change";
  outputs?: string[];
  mock?: boolean;
}

export interface TypeToConfirmProps {
  /** The name to type: the environment name. */
  name: string;
  /** "apply" or "destroy" — the submit says exactly what happens. */
  verb: "apply" | "destroy";
  /** Why the friction applies ("four-eyes tier prod", "destroy run"). */
  because: string[];
  counts: PlanCounts | null;
  destroyed: string[];
  replaced: string[];
  cascade: CascadeItem[];
  pending?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function TypeToConfirm({
  name,
  verb,
  because,
  counts,
  destroyed,
  replaced,
  cascade,
  pending,
  onConfirm,
  onCancel,
}: TypeToConfirmProps) {
  const [typed, setTyped] = useState("");
  const inputId = useId();
  const ok = typed === name;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (ok && !pending) onConfirm();
  };
  return (
    <form onSubmit={submit} className="flex w-[min(440px,calc(100vw-48px))] flex-col gap-3">
      <div>
        <div className="text-section font-semibold">
          Confirm {verb} on <span className="font-data">{name}</span>
        </div>
        <div className="text-[12px] text-muted">Required because: {because.join(" · ")}</div>
      </div>
      {counts && <PlanCountsBig counts={counts} />}
      {(destroyed.length > 0 || replaced.length > 0) && (
        <div className="max-h-48 overflow-auto rounded-base border border-st-failed bg-tint-failed px-2 py-1.5">
          {destroyed.length > 0 && (
            <>
              <div className="eyebrow text-st-failed">− Destroyed · {destroyed.length}</div>
              <ul className="mb-1 font-data text-[12px]">
                {destroyed.map((a) => (
                  <li key={a} className="break-all">
                    {a}
                  </li>
                ))}
              </ul>
            </>
          )}
          {replaced.length > 0 && (
            <>
              <div className="eyebrow text-st-failed">± Replaced · {replaced.length}</div>
              <ul className="font-data text-[12px]">
                {replaced.map((a) => (
                  <li key={a} className="break-all">
                    {a}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
      {cascade.length > 0 && (
        <div className="text-[12px]">
          <div className="eyebrow mb-0.5 text-muted">This {verb} can trigger</div>
          <ul className="font-data">
            {cascade.map((c) => (
              <li key={c.label}>
                {c.label}{" "}
                <span className="text-muted">
                  {c.likelihood === "will_plan"
                    ? "— will plan"
                    : `— plans if ${c.outputs?.join(", ") || "outputs"} change`}
                </span>
                {c.mock && <span className="text-mocked"> · ◆ mock defined</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      <label htmlFor={inputId} className="flex flex-col gap-1">
        <span className="text-[12px]">
          Type <span className="font-data font-semibold">{name}</span> to confirm
        </span>
        <Input
          id={inputId}
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          autoFocus
        />
      </label>
      <div className="flex flex-wrap justify-end gap-2">
        <Button variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          type="submit"
          variant={verb === "destroy" ? "danger" : "decision"}
          disabled={!ok}
          loading={pending}
        >
          Confirm {verb} on {name}
        </Button>
      </div>
    </form>
  );
}
