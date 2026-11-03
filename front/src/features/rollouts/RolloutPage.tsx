import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ApiError } from "@/api/client";
import { rollouts, type Rollout } from "@/api/resources";
import { useEntityStream } from "@/api/stream";
import { useEntitySpace, useSpaces } from "@/app/SpaceContext";
import { Button } from "@/ui/Button";
import { ConfirmPopover } from "@/ui/ConfirmPopover";
import { TypeNameDialog } from "@/ui/TypeNameDialog";
import { Badge, Banner, EmptyState, RelativeTime, Skeleton } from "@/ui/primitives";
import { MEMBER_STATUS, PAUSE_REASON, ROLLOUT_STATE, byLevel, progress, summaryText } from "./rolloutModel";

// A rollout (SPECS §21, DESIGN §5.11): the members level by level, why each one waits, and the one
// decision the page exists for — the approval.

function errorText(e: unknown): string {
  return e instanceof ApiError ? (e.detail ?? e.title) : String(e);
}

function ApproveDialog({ r, onDone }: { r: Rollout; onDone: () => void }) {
  const tiers = [...new Set(r.members.map((m) => m.tier))].join(", ");
  return (
    <TypeNameDialog
      trigger={<Button variant="decision">{r.state === "paused" ? "Approve again…" : "Approve…"}</Button>}
      title={`Approve the rollout of ${r.name}?`}
      consequence={
        <div className="flex flex-col gap-2">
          <p>
            For 24 hours, every plan of these environments that <strong>only creates</strong> resources is
            applied without asking again. Any plan that changes, destroys or imports something, consumes a
            mock or raises a warning stops for a human confirmation.
          </p>
          <ul className="font-data text-[12px]">
            {r.members.map((m) => (
              <li key={m.environment_id}>
                {m.stack_name}/{m.env_name} · {m.tier}
                {m.protected ? " · protected" : ""}
              </li>
            ))}
          </ul>
          <p className="text-[12px] text-muted">
            Tiers: {tiers}. Your approval is recorded as the confirmer.
          </p>
        </div>
      }
      name={r.name}
      confirmLabel="Approve the rollout"
      variant="decision"
      onConfirm={() => rollouts.approve(r.id).then(onDone)}
    />
  );
}

export function RolloutPage() {
  const { rolloutId = "" } = useParams();
  const qc = useQueryClient();
  const { list: spaces } = useSpaces();
  const key = ["rollout", rolloutId];
  useEntityStream(`rollout:${rolloutId}`, [key]);
  const { data: r, error, isLoading } = useQuery({ queryKey: key, queryFn: () => rollouts.get(rolloutId) });
  useEntitySpace(r?.space_id);
  const invalidate = () => qc.invalidateQueries({ queryKey: key });
  const retry = useMutation({ mutationFn: () => rollouts.retry(rolloutId), onSuccess: invalidate });
  const cancel = useMutation({ mutationFn: () => rollouts.cancel(rolloutId), onSuccess: invalidate });

  if (isLoading) return <Skeleton className="h-40" />;
  if (error || !r)
    return (
      <EmptyState title="Rollout not found.">
        {error ? errorText(error) : "It may have been removed."}
      </EmptyState>
    );

  const space = spaces.find((s) => s.id === r.space_id);
  const envHref = (stack: string, env: string) =>
    space
      ? `/s/${encodeURIComponent(space.name)}/stacks/${encodeURIComponent(stack)}/envs/${encodeURIComponent(env)}`
      : "#";
  const state = ROLLOUT_STATE[r.state];
  const over = r.state === "succeeded" || r.state === "failed" || r.state === "canceled";
  const needsApproval =
    r.state === "awaiting_approval" ||
    (r.state === "paused" &&
      (r.pause_reason === "approval_expired" || r.pause_reason === "approver_lost_rights"));

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-col gap-1">
        <h1 className="flex flex-wrap items-center gap-2 text-title font-semibold">
          Rollout <span className="font-data">{r.name}</span>
          <Badge tone={state.tone} glyph={state.glyph}>
            {state.label}
          </Badge>
          <span className="font-data text-[12px] font-normal text-muted">{progress(r)}</span>
        </h1>
        <p className="flex flex-wrap gap-x-2 text-[12px] text-muted">
          <span>created by {r.created_by.email}</span>
          <span>
            · <RelativeTime value={r.created_at} />
          </span>
          {r.approved_by && (
            <span>
              · approved by {r.approved_by.email}
              {r.approval_expires_at && !over && (
                <>
                  {" "}
                  · approval expires <RelativeTime value={r.approval_expires_at} />
                </>
              )}
            </span>
          )}
        </p>
      </header>

      {r.state === "paused" && r.pause_reason && (
        <Banner tone="decision" title="Paused">
          {PAUSE_REASON[r.pause_reason]}
        </Banner>
      )}
      {r.state === "awaiting_approval" && (
        <Banner tone="decision" title="Awaiting approval">
          Nothing runs before an approver approves it. Below: the order the environments will go in.
        </Banner>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {needsApproval &&
          (r.can_approve.allowed ? (
            <ApproveDialog r={r} onDone={invalidate} />
          ) : (
            <span className="text-[12px] text-muted">You can&apos;t approve it: {r.can_approve.reason}</span>
          ))}
        {r.state === "failed" && (
          <Button loading={retry.isPending} onClick={() => retry.mutate()}>
            Retry the failed environments
          </Button>
        )}
        {!over && (
          <ConfirmPopover
            trigger={<Button variant="ghost">Cancel rollout</Button>}
            question="Cancel this rollout? Queued and unconfirmed runs are canceled; a run in progress finishes on its own."
            confirmLabel="Cancel the rollout"
            onConfirm={() => cancel.mutate()}
          />
        )}
      </div>
      {(retry.error || cancel.error) && (
        <Banner tone="failed" role="alert" title="The API refused">
          {errorText(retry.error ?? cancel.error)}
        </Banner>
      )}

      <ol className="flex flex-wrap gap-3" aria-label="Environments by level">
        {byLevel(r.members).map((level, i) => (
          <li key={i} className="flex min-w-[240px] flex-1 flex-col gap-2">
            <h2 className="text-[12px] font-semibold uppercase text-muted">
              Level {i}
              {i === 0 ? " · no upstream in the rollout" : ""}
            </h2>
            <ul className="flex flex-col gap-2">
              {level.map((m) => {
                const st = MEMBER_STATUS[m.status];
                const sum = summaryText(m);
                return (
                  <li key={m.environment_id} className="rounded-base border border-line bg-surface px-3 py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link className="link font-data font-semibold" to={envHref(m.stack_name, m.env_name)}>
                        {m.stack_name}/{m.env_name}
                      </Link>
                      <span className="font-data text-[12px] text-muted">
                        {m.tier}
                        {m.protected ? " · protected" : ""}
                      </span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px]">
                      <Badge tone={st.tone} glyph={st.glyph}>
                        {st.label}
                      </Badge>
                      {sum && <span className="font-data">{sum}</span>}
                      {m.run_id && (
                        <Link className="link" to={`/runs/${m.run_id}`}>
                          run
                        </Link>
                      )}
                    </div>
                    {m.waiting_reason && <p className="mt-1 text-[12px]">Why it waits: {m.waiting_reason}</p>}
                    {m.auto_confirmed && (
                      <p className="mt-1 text-[12px] text-muted">Confirmed under the rollout approval.</p>
                    )}
                    {m.expected_mocks.length > 0 && m.status === "pending" && (
                      <p className="mt-1 text-[12px] text-muted">
                        Will plan on a mock ({m.expected_mocks.join(", ")}): it will stop for a human.
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function RolloutsPage() {
  const { current: space } = useSpaces();
  const { data, isLoading } = useQuery({
    queryKey: ["rollouts", space?.id],
    queryFn: () => rollouts.list(space!.id),
    enabled: !!space,
  });
  if (!space || isLoading) return <Skeleton className="h-40" />;
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-title font-semibold">
        Rollouts <span className="font-data text-[13px] font-normal text-muted">{space.name}</span>
      </h1>
      {!data?.length ? (
        <EmptyState title="No rollout yet.">
          Declare an environment with “New environment from…” on the overview, then roll it out.
        </EmptyState>
      ) : (
        <ul className="flex flex-col gap-2">
          {data.map((r) => {
            const st = ROLLOUT_STATE[r.state];
            return (
              <li
                key={r.id}
                className="flex flex-wrap items-center gap-3 rounded-base border border-line bg-surface px-3 py-2"
              >
                <Link className="link font-data font-semibold" to={`/rollouts/${r.id}`}>
                  {r.name}
                </Link>
                <Badge tone={st.tone} glyph={st.glyph}>
                  {st.label}
                </Badge>
                <span className="font-data text-[12px] text-muted">
                  {r.done} / {r.members} done
                </span>
                <span className="text-[12px] text-muted">
                  <RelativeTime value={r.created_at} />
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
