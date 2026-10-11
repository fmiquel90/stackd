import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { MessageSquarePlus } from "lucide-react";
import { ApiError } from "@/api/client";
import { runs } from "@/api/resources";
import type { RunComment } from "@/api/types";
import { useSpaces } from "@/app/SpaceContext";
import { useHotkeys } from "@/app/keyboard";
import { useSpaceEnvs } from "@/features/data/spaceEnvs";
import { LogViewer } from "@/identity/LogViewer";
import { PhaseRail } from "@/identity/PhaseRail";
import { PlanDiff } from "@/identity/PlanDiff";
import { ProvenanceBadge, isNotReal, parseProvenance } from "@/identity/ProvenanceBadge";
import { RunActionBar } from "@/identity/RunActionBar";
import { buildLogModel } from "@/lib/logModel";
import { runRights, whoMayConfirm } from "@/lib/permissions";
import { buildSegments, checkEntries, failedSegment, type Segment } from "@/lib/phases";
import { formatCounts, outcomeOf, planCounts } from "@/lib/runOutcome";
import { formatDuration } from "@/lib/time";
import { Button } from "@/ui/Button";
import {
  Banner,
  Badge,
  EmptyState,
  RelativeTime,
  Sha,
  Skeleton,
  Table,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Td,
  Th,
} from "@/ui/primitives";
import { CheckList } from "./CheckList";
import { CommentComposer, CommentThread, Discussion, commentLineKey, isRoot, lineKey } from "./comments";
import { useRunPage } from "./useRunPage";

// The run page (DESIGN §5.2): the decision first, then the evidence — plan, checks, logs, inputs,
// discussion. Every piece of on-screen state that matters is in the URL (?tab, ?line, ?plan_q).

type Tab = "plan" | "checks" | "logs" | "inputs" | "discussion";
const TABS: Tab[] = ["plan", "checks", "logs", "inputs", "discussion"];
const SEGMENT_NAME: Record<string, string> = {
  prepare: "Prepare",
  plan: "Plan",
  check: "Check",
  apply: "Apply",
  run: "Run",
  decision: "Decision",
};
const REASONS: Record<string, string> = {
  active_run: "another run is active on this environment",
  env_locked: "the environment is locked",
  no_compatible_worker: "no worker is compatible with this environment",
  apply_affinity_hold: "the apply is reserved for the worker that planned it (affinity window)",
};

const durationOf = (from: string, to: string | null) =>
  formatDuration((new Date(to ?? Date.now()).getTime() - new Date(from).getTime()) / 1000);

export function RunPage() {
  const { runId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const d = useRunPage(runId);
  const space = useSpaceEnvs(d.stack?.space_id, d.me);
  const { list: spaces } = useSpaces();
  const [resourceDraft, setResourceDraft] = useState<string | null>(null);
  const [lineDraft, setLineDraft] = useState<{
    phase: string;
    seq: number;
    idx: number;
    snippet: string;
  } | null>(null);

  const { run, env, stack, me } = d;
  const model = useMemo(() => buildLogModel(d.logs), [d.logs]);

  const rights = run && env && me ? runRights(run, env, d.tier, me) : null;
  if (rights && run && run.type !== "tracked" && run.type !== "destroy") rights.replan.visible = false;
  const outcome = run ? outcomeOf(run) : null;

  const defaultTab: Tab = !run
    ? "plan"
    : run.state === "failed"
      ? "logs"
      : ["queued", "preparing", "planning", "checking", "confirmed", "applying", "running"].includes(
            run.state,
          )
        ? "logs"
        : "plan";
  const tab = (TABS as string[]).includes(params.get("tab") ?? "") ? (params.get("tab") as Tab) : defaultTab;
  const setParam = (patch: Record<string, string | null>, replace = true) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    setParams(next, { replace });
  };

  /* ---------- actions ---------- */
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["run", runId] });
    if (env) qc.invalidateQueries({ queryKey: ["env-runs", env.id] });
  };
  const confirm = useMutation({ mutationFn: () => runs.confirm(runId), onSuccess: refresh });
  const discard = useMutation({ mutationFn: () => runs.discard(runId), onSuccess: refresh });
  const cancel = useMutation({ mutationFn: () => runs.cancel(runId), onSuccess: refresh });
  const replan = useMutation({
    mutationFn: async () => {
      if (!run || !env) throw new Error("run not loaded");
      if (run.state === "unconfirmed") await runs.discard(run.id);
      return runs.trigger(env.id, run.type === "destroy" ? "destroy" : "tracked");
    },
    onSuccess: (r) => navigate(`/runs/${r.id}`),
  });
  const failure = [confirm, discard, cancel, replan].find((m) => m.error)?.error;
  const apiError =
    failure instanceof ApiError ? (failure.detail ?? failure.title) : failure ? String(failure) : null;

  /* ---------- next decision ---------- */
  const decisions = space.needsYou.map((e) => e.summary.primary!.id);
  const at = decisions.indexOf(runId);
  const others = decisions.filter((id) => id !== runId);
  const nextDecision = at >= 0 ? decisions[at + 1] : others[0];
  const prevDecision = at > 0 ? decisions[at - 1] : undefined;

  useHotkeys(
    {
      c: () => document.querySelector<HTMLButtonElement>('[data-action="confirm"]:not(:disabled)')?.click(),
      d: () => document.querySelector<HTMLButtonElement>('[data-action="discard"]:not(:disabled)')?.click(),
      x: () => document.querySelector<HTMLButtonElement>('[data-action="cancel"]:not(:disabled)')?.click(),
      "]": () => nextDecision && navigate(`/runs/${nextDecision}?tab=plan`),
      "[": () => prevDecision && navigate(`/runs/${prevDecision}?tab=plan`),
      ...Object.fromEntries(TABS.map((t, i) => [String(i + 1), () => setParam({ tab: t })])),
    },
    !!run,
  );

  if (d.runError instanceof ApiError && (d.runError.status === 404 || d.runError.status === 403)) {
    return (
      <EmptyState title="Run not found">
        This run does not exist, or it belongs to a space you are not a member of.{" "}
        <Link to="/" className="link">
          Go to the overview
        </Link>
      </EmptyState>
    );
  }
  if (!run || !env || !rights || !me) return <RunSkeleton />;

  const counts = planCounts(run);
  const confirmerEmail = d.emailOf(run.confirmed_by_user_id);
  const triggererEmail = d.emailOf(run.trigger_user_id);
  const segments = buildSegments(run, model.durations, confirmerEmail ?? undefined);
  const destroyed = d.parsed?.resources.filter((r) => r.action === "destroy").map((r) => r.address) ?? [];
  const replaced = d.parsed?.resources.filter((r) => r.action === "replace").map((r) => r.address) ?? [];
  const checks = checkEntries(run.check_results);
  const failing = checks.find((c) => c.status === "fail");
  const failedHooks = checks.filter((c) => c.status === "fail").map((c) => c.name);
  const provenance = Object.entries(run.variable_provenance ?? {}).map(([name, p]) => ({
    name,
    p: parseProvenance(p),
  }));
  const mocked = provenance.filter((x) => x.p.kind === "mock").map((x) => x.name);
  const openThreads = d.threads.filter((c) => isRoot(c) && !c.resolved).length;
  const stale =
    run.state === "unconfirmed" &&
    !!env.head_sha &&
    !!run.commit_sha &&
    !env.head_sha.startsWith(run.commit_sha) &&
    !run.commit_sha.startsWith(env.head_sha);
  const spaceName = spaces.find((s) => s.id === stack?.space_id)?.name;
  const envPath =
    stack && spaceName
      ? `/s/${encodeURIComponent(spaceName)}/stacks/${encodeURIComponent(stack.name)}/envs/${encodeURIComponent(env.name)}`
      : null;
  // Where a failed run's logs should open: the first error line, else the failing hook's section.
  const failingHookStart = model.sections.find(
    (sec) => sec.section && failedHooks.some((h) => sec.section === `hook:${h}`),
  );
  const firstErrorId =
    model.firstError >= 0
      ? model.lines[model.firstError].id
      : failingHookStart
        ? (model.lines[failingHookStart.start]?.id ?? null)
        : null;
  const openSegment = (s: Segment) =>
    setParam({ tab: "logs", line: model.lines.find((l) => l.phase === s.logPhase)?.id ?? null }, false);
  const threadsByLine = new Map<string, RunComment[]>();
  for (const c of d.threads) {
    const k = isRoot(c) ? commentLineKey(c) : null;
    if (k) threadsByLine.set(k, [...(threadsByLine.get(k) ?? []), c]);
  }
  const repliesOf = (id: string) => d.threads.filter((c) => c.parent_id === id);
  const resourceThreads = (address: string) =>
    d.threads.filter((c) => isRoot(c) && c.anchor?.kind === "resource" && c.anchor.address === address);

  return (
    <div className="-m-4 flex flex-col pb-24 md:pb-0 lg:-m-6">
      {/* ---------- identity ---------- */}
      <header className="flex flex-col gap-1 px-4 pt-3 pb-2 lg:px-6">
        <h1 className="flex flex-wrap items-baseline gap-x-2 text-title font-semibold">
          {envPath ? (
            <Link to={envPath} className="hover:underline">
              {stack?.name} / {env.name}
            </Link>
          ) : (
            "…"
          )}
          <span className="font-data text-[13px] font-normal text-muted">run {run.id.slice(0, 8)}</span>
          {run.type !== "tracked" && (
            <Badge
              tone={run.type === "destroy" ? "failed" : "neutral"}
              glyph={run.type === "destroy" ? "−" : undefined}
            >
              {run.type === "proposed" ? "PR plan" : run.type}
            </Badge>
          )}
        </h1>
        <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px] text-muted">
          <Sha value={run.commit_sha} copy />
          {run.commit_message && <span className="text-primary">“{run.commit_message}”</span>}
          {run.commit_author && <span>· {run.commit_author}</span>}
          <span>
            · branch <span className="font-data">{env.branch}</span>
          </span>
          <span>
            ·{" "}
            {run.triggered_by === "manual"
              ? `triggered by ${triggererEmail ?? "a user"}`
              : `via ${run.triggered_by === "schedule" ? "drift check" : run.triggered_by}`}
          </span>
          <span>
            · <RelativeTime value={run.created_at} />
          </span>
          <span className="font-data">· {durationOf(run.created_at, run.finished_at)}</span>
          {run.command && (
            <span className="font-data">
              · {run.command.name} {run.command.args.join(" ")}
            </span>
          )}
        </p>
      </header>

      {/* ---------- decision ---------- */}
      <RunActionBar
        className="sticky top-0 z-20"
        run={run}
        env={env}
        rights={rights}
        me={me}
        triggerer={triggererEmail}
        confirmer={confirmerEmail}
        whoMayConfirm={whoMayConfirm(run, env, rights, triggererEmail ?? undefined)}
        cascade={d.cascade}
        destroyed={destroyed}
        replaced={replaced}
        instanceCaveat={spaces.length > 1}
        pending={{
          confirm: confirm.isPending,
          discard: discard.isPending,
          cancel: cancel.isPending,
          replan: replan.isPending,
        }}
        apiError={apiError}
        onConfirm={() => confirm.mutate()}
        onDiscard={() => discard.mutate()}
        onCancel={() => cancel.mutate()}
        onReplan={() => replan.mutate()}
        prevDecision={prevDecision ? `/runs/${prevDecision}?tab=plan` : null}
        nextDecision={nextDecision ? `/runs/${nextDecision}?tab=plan` : null}
      />

      {/* ---------- banners ---------- */}
      <div className="flex flex-col gap-2 px-4 pt-3 lg:px-6">
        {outcome === "failed_during_apply" && (
          <Banner
            tone="failed"
            role="alert"
            title="Failed during apply — infrastructure may be partially changed."
            action={
              firstErrorId && (
                <Button size="sm" onClick={() => setParam({ tab: "logs", line: firstErrorId }, false)}>
                  Jump to first error
                </Button>
              )
            }
          >
            {run.error}
          </Banner>
        )}
        {outcome === "failed" && (
          <Banner
            tone="failed"
            title={`Failed at ${
              SEGMENT_NAME[
                failedSegment(
                  run,
                  segments.map((s) => s.key),
                )
              ]
            } — nothing was applied.`}
            action={
              firstErrorId && (
                <Button size="sm" onClick={() => setParam({ tab: "logs", line: firstErrorId }, false)}>
                  Jump to first error
                </Button>
              )
            }
          >
            {failing ? (
              <>
                Check <span className="font-data">{failing.name}</span> failed
                {failing.stage ? ` at ${failing.stage}` : ""}
                {failing.detail ? `: ${failing.detail}` : "."}
              </>
            ) : (
              run.error
            )}
          </Banner>
        )}
        {stale && (
          <Banner
            title={
              <>
                Plan computed on <span className="font-data">{run.commit_sha!.slice(0, 7)}</span>. The branch
                head is now <span className="font-data">{env.head_sha!.slice(0, 7)}</span>
                {env.commits_ahead
                  ? ` (${env.commits_ahead} commit${env.commits_ahead > 1 ? "s" : ""} ahead)`
                  : " (ahead)"}
                .
              </>
            }
            action={
              rights.replan.visible && (
                <Button
                  size="sm"
                  disabled={rights.replan.reasons.length > 0}
                  loading={replan.isPending}
                  onClick={() => replan.mutate()}
                >
                  Re-plan on head
                </Button>
              )
            }
          >
            Confirming still applies exactly <span className="font-data">{run.commit_sha!.slice(0, 7)}</span>.
            Re-plan discards this plan and plans the head.
          </Banner>
        )}
        {run.used_mocks && (
          <Banner tone="mock" title="This plan used mock values — it is not real.">
            Mocked inputs: <span className="font-data">{mocked.join(", ") || "see Inputs"}</span>.{" "}
            {env.allow_mock_apply
              ? "This environment allows applying a mocked plan."
              : "A mocked plan cannot be applied here (allow_mock_apply is off)."}
          </Banner>
        )}
        {run.used_secret_fallback && (
          <Banner tone="decision" title="A secret came from a fallback, not the real source.">
            {env.allow_fallback_apply
              ? "This environment allows applying it."
              : "Apply is disabled (allow_fallback_apply is off)."}
          </Banner>
        )}
        {d.queueEntry?.blocking_reason && (
          <Banner
            title={`Waiting: ${REASONS[d.queueEntry.blocking_reason] ?? d.queueEntry.blocking_reason}.`}
          >
            <Link to="/queue" className="link">
              Open the queue
            </Link>
          </Banner>
        )}
      </div>

      {/* ---------- evidence ---------- */}
      <div className="flex gap-6 px-4 pt-3 pb-6 lg:px-6">
        <aside className="hidden w-44 shrink-0 lg:block">
          <div className="sticky top-40">
            <PhaseRail segments={segments} onSelect={openSegment} />
          </div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col gap-3">
          <PhaseRail variant="horizontal" segments={segments} onSelect={openSegment} className="lg:hidden" />
          <Tabs value={tab} onValueChange={(v) => setParam({ tab: v, line: null }, false)}>
            <TabsList>
              <TabsTrigger value="plan">
                Plan{" "}
                <span className="font-data text-[12px] text-muted">
                  {counts ? formatCounts(counts) : "—"}
                </span>
              </TabsTrigger>
              <TabsTrigger value="checks">
                Checks <span className="font-data text-[12px] text-muted">{checkCounts(checks)}</span>
              </TabsTrigger>
              <TabsTrigger value="logs">
                Logs
                {model.errorCount > 0 && (
                  <span className="font-data text-[12px] text-st-failed">✕ {model.errorCount}</span>
                )}
              </TabsTrigger>
              <TabsTrigger value="inputs">
                Inputs{" "}
                {mocked.length > 0 && (
                  <span className="font-data text-[12px] text-mocked">◆ {mocked.length}</span>
                )}
              </TabsTrigger>
              <TabsTrigger value="discussion">
                Discussion{" "}
                {openThreads > 0 && (
                  <span className="font-data text-[12px] text-muted">{openThreads} open</span>
                )}
              </TabsTrigger>
            </TabsList>

            <TabsContent value="plan" className="pt-3">
              <h2 className="sr-only">Plan</h2>
              {resourceDraft && (
                <div className="mb-3">
                  <Banner
                    title={
                      <>
                        Comment on <span className="font-data">{resourceDraft}</span>
                      </>
                    }
                  >
                    <CommentComposer
                      runId={run.id}
                      anchor={{
                        kind: "resource",
                        address: resourceDraft,
                        action: d.parsed?.resources.find((r) => r.address === resourceDraft)?.action,
                      }}
                      placeholder="Comment on this resource…"
                      autoFocus
                      onPosted={() => setResourceDraft(null)}
                      onCancel={() => setResourceDraft(null)}
                    />
                  </Banner>
                </div>
              )}
              <PlanDiff
                lines={d.planText}
                counts={counts}
                query={params.get("plan_q") ?? ""}
                onQueryChange={(q) => setParam({ plan_q: q })}
                emptyHint={
                  <>
                    {run.state === "failed"
                      ? "The run failed before the plan output was produced."
                      : ["queued", "preparing", "planning"].includes(run.state)
                        ? "The plan is not computed yet."
                        : "This run produced no plan output."}{" "}
                    <button type="button" className="link" onClick={() => setParam({ tab: "logs" }, false)}>
                      Open the logs
                    </button>
                  </>
                }
                resourceAction={(r) => {
                  const n = resourceThreads(r.address).length;
                  return (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={(e) => {
                        e.preventDefault();
                        setResourceDraft(r.address);
                      }}
                      icon={<MessageSquarePlus size={13} aria-hidden />}
                      aria-label={`Comment on ${r.address}`}
                    >
                      {n > 0 ? `${n}` : ""}
                    </Button>
                  );
                }}
              />
            </TabsContent>

            <TabsContent value="checks" className="pt-3">
              <h2 className="sr-only">Checks</h2>
              {checks.length ? (
                <CheckList checkResults={run.check_results} />
              ) : (
                <EmptyState title="No hook checks ran for this run." />
              )}
            </TabsContent>

            <TabsContent value="logs" className="pt-3">
              <h2 className="sr-only">Logs</h2>
              <LogViewer
                chunks={d.logs}
                live={!outcome}
                failedHooks={failedHooks}
                initialLine={params.get("line") ?? (run.state === "failed" ? firstErrorId : null)}
                onLineLink={(id) => {
                  setParam({ tab: "logs", line: id });
                  const url = new URL(window.location.href);
                  url.searchParams.set("tab", "logs");
                  url.searchParams.set("line", id);
                  void navigator.clipboard?.writeText(url.toString());
                }}
                lineAction={(l) => (
                  <button
                    type="button"
                    aria-label={`Comment on line ${l.id}`}
                    className="rounded-base p-0.5 text-muted hover:bg-raised hover:text-primary"
                    onClick={() =>
                      setLineDraft({ phase: l.phase, seq: l.seq, idx: l.idx, snippet: l.msg.slice(0, 120) })
                    }
                  >
                    <MessageSquarePlus size={13} strokeWidth={1.5} aria-hidden />
                  </button>
                )}
                renderAfterLine={(l) => {
                  const k = lineKey(l.phase, l.seq, l.idx);
                  const roots = threadsByLine.get(k) ?? [];
                  const drafting = lineDraft && lineKey(lineDraft.phase, lineDraft.seq, lineDraft.idx) === k;
                  if (!roots.length && !drafting) return null;
                  return (
                    <div className="my-1 ml-14 flex flex-col gap-2 border-l-2 border-line pl-3 font-sans whitespace-normal">
                      {roots.map((root) => (
                        <CommentThread
                          key={root.id}
                          runId={run.id}
                          root={root}
                          replies={repliesOf(root.id)}
                          meId={me.id}
                          hideAnchor
                        />
                      ))}
                      {drafting && (
                        <CommentComposer
                          runId={run.id}
                          anchor={{
                            kind: "plan_line",
                            phase: l.phase,
                            seq: l.seq,
                            line_start: l.idx,
                            line_end: l.idx,
                            snippet: lineDraft.snippet,
                          }}
                          placeholder="Comment on this line…"
                          autoFocus
                          onPosted={() => setLineDraft(null)}
                          onCancel={() => setLineDraft(null)}
                        />
                      )}
                    </div>
                  );
                }}
                className="h-[70vh] min-h-[420px]"
              />
            </TabsContent>

            <TabsContent value="inputs" className="pt-3">
              <h2 className="sr-only">Inputs</h2>
              {provenance.length ? (
                <div className="max-w-[860px]">
                  <Table>
                    <thead>
                      <tr>
                        <Th>Variable</Th>
                        <Th>Provenance</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...provenance]
                        .sort(
                          (a, b) =>
                            Number(isNotReal(b.p)) - Number(isNotReal(a.p)) || a.name.localeCompare(b.name),
                        )
                        .map((x) => (
                          <tr key={x.name}>
                            <Td className="font-data text-[12px]">{x.name}</Td>
                            <Td>
                              <ProvenanceBadge provenance={x.p} />
                            </Td>
                          </tr>
                        ))}
                    </tbody>
                  </Table>
                  <p className="mt-2 text-[12px] text-muted">
                    Where each input came from when this run resolved it. Values are not kept on the run;
                    sensitive values are never shown anywhere.
                  </p>
                </div>
              ) : (
                <EmptyState title="No inputs recorded for this run." />
              )}
            </TabsContent>

            <TabsContent value="discussion" className="pt-3">
              <h2 className="sr-only">Discussion</h2>
              <Discussion runId={run.id} threads={d.threads} meId={me.id} />
            </TabsContent>
          </Tabs>
        </div>
      </div>
    </div>
  );
}

function checkCounts(checks: { status: string }[]): string {
  if (!checks.length) return "—";
  const n = (s: string) => checks.filter((c) => c.status === s).length;
  return [n("ok") && `${n("ok")} ok`, n("warn") && `${n("warn")} warn`, n("fail") && `${n("fail")} fail`]
    .filter(Boolean)
    .join(" · ");
}

function RunSkeleton() {
  return (
    <div className="flex flex-col gap-3" aria-busy="true">
      <span className="sr-only">Loading the run…</span>
      <Skeleton className="h-6 w-80" />
      <Skeleton className="h-4 w-[32rem] max-w-full" />
      <Skeleton className="h-28 w-full" />
      <div className="flex gap-6">
        <Skeleton className="hidden h-60 w-44 lg:block" />
        <Skeleton className="h-60 flex-1" />
      </div>
    </div>
  );
}
