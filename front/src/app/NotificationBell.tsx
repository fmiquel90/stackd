import { useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Bell, X } from "lucide-react";
import { inbox, runs, stacks } from "@/api/resources";
import { useEntityStream } from "@/api/stream";
import type { Run, UserNotification } from "@/api/types";
import { relative } from "@/lib/time";
import { Button } from "@/ui/Button";
import { cn } from "@/ui/cn";
import { Popover, PopoverContent, PopoverTrigger } from "@/ui/overlays";

// The in-app inbox (SPECS §17, DESIGN §5.10): each item says what and where ("Run failed ·
// demo-dns/dev · 6m") and opens the run — at its first error for a failure. Opening marks all read;
// the unread badge is neutral (amber is reserved for the decisions themselves, violet for focus).

const WHAT: Record<UserNotification["kind"], string> = {
  approval_request: "Awaiting your decision",
  run_finished: "Run finished",
  run_failed: "Run failed",
  comment_reply: "Reply to your thread",
  mention: "You were mentioned",
};

function hrefOf(n: UserNotification): string | null {
  if (!n.run_id) return null;
  if (n.kind === "run_failed") return `/runs/${n.run_id}?tab=logs`;
  if (n.kind === "comment_reply" || n.kind === "mention") return `/runs/${n.run_id}?tab=discussion`;
  return `/runs/${n.run_id}`;
}

export function NotificationBell({ userId }: { userId: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  // Items unread when the popover opened keep their "new" mark while it stays open.
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  useEntityStream(`user:${userId}`, [["notifications"]]);
  const { data } = useQuery({ queryKey: ["notifications"], queryFn: inbox.list });
  const items = data ?? [];
  const unread = items.filter((n) => !n.read).length;

  // Where: the run's environment, resolved only while the list is shown.
  const runQueries = useQueries({
    queries: items.map((n) => ({
      queryKey: ["run", n.run_id],
      queryFn: () => runs.get(n.run_id!),
      enabled: open && !!n.run_id,
      staleTime: 60_000,
    })),
  });
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list, enabled: open });
  const envQueries = useQueries({
    queries: (open ? (stackList.data ?? []) : []).map((s) => ({
      queryKey: ["environments", s.id],
      queryFn: () => stacks.environments(s.id),
    })),
  });
  const envs = envQueries.flatMap((q) => q.data ?? []);
  const whereOf = (n: UserNotification, run: Run | undefined): string | null => {
    const envId =
      (typeof n.context?.environment_id === "string" && n.context.environment_id) || run?.environment_id;
    const env = envId ? envs.find((e) => e.id === envId) : undefined;
    const stack = env && stackList.data?.find((s) => s.id === env.stack_id);
    if (env && stack) return `${stack.name}/${env.name}`;
    return typeof n.context?.environment === "string" ? n.context.environment : null;
  };

  const invalidate = () => qc.invalidateQueries({ queryKey: ["notifications"] });
  const markRead = useMutation({ mutationFn: () => inbox.markRead(), onSuccess: invalidate });
  const remove = useMutation({ mutationFn: (id: string) => inbox.remove(id), onSuccess: invalidate });
  const clearRead = useMutation({ mutationFn: () => inbox.clearRead(), onSuccess: invalidate });

  const onOpenChange = (next: boolean) => {
    setOpen(next);
    if (next) {
      setFresh(new Set(items.filter((n) => !n.read).map((n) => n.id)));
      if (unread > 0) markRead.mutate();
    }
  };

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Notifications${unread ? `, ${unread} unread` : ""}`}
          className="relative flex size-8 items-center justify-center rounded-base text-muted hover:bg-raised hover:text-primary"
        >
          <Bell size={17} strokeWidth={1.5} aria-hidden />
          {unread > 0 && (
            <span
              aria-hidden
              className="absolute -top-0.5 -right-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-badge border border-control bg-raised px-0.5 font-data text-[11px] leading-none text-primary"
            >
              {unread > 9 ? "9+" : unread}
            </span>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        aria-label="Notifications"
        className="flex w-[360px] max-w-[calc(100vw-32px)] flex-col gap-2 p-2"
      >
        <div className="flex items-center justify-between gap-2 px-1">
          <h2 className="text-ui font-semibold">Notifications</h2>
          {items.some((n) => n.read && !fresh.has(n.id)) && (
            <Button
              size="sm"
              variant="ghost"
              loading={clearRead.isPending}
              onClick={() => clearRead.mutate()}
            >
              Clear read
            </Button>
          )}
        </div>
        {items.length === 0 ? (
          <p className="px-2 py-3 text-[12px] text-muted">Nothing yet.</p>
        ) : (
          <ul className="flex max-h-[400px] flex-col gap-0.5 overflow-auto">
            {items.map((n, i) => {
              const href = hrefOf(n);
              const where = whereOf(n, runQueries[i]?.data);
              const isNew = fresh.has(n.id);
              const body = (
                <>
                  <span className="text-[13px]">
                    {isNew && (
                      <>
                        <span aria-hidden className="font-data">
                          •{" "}
                        </span>
                        <span className="sr-only">New: </span>
                      </>
                    )}
                    {WHAT[n.kind]}
                    {n.kind === "mention" && typeof n.context?.by === "string" && ` by ${n.context.by}`}
                  </span>
                  <span className="font-data text-[11px] text-muted">
                    {[where, relative(n.created_at)].filter(Boolean).join(" · ")}
                  </span>
                </>
              );
              return (
                <li key={n.id} className={cn("flex items-start gap-1 rounded-base", isNew && "bg-raised")}>
                  {href ? (
                    <Link
                      to={href}
                      onClick={() => setOpen(false)}
                      className="flex min-w-0 flex-1 flex-col rounded-base px-2 py-1.5 hover:bg-raised"
                    >
                      {body}
                    </Link>
                  ) : (
                    <div className="flex min-w-0 flex-1 flex-col px-2 py-1.5">{body}</div>
                  )}
                  <button
                    type="button"
                    aria-label={`Dismiss: ${WHAT[n.kind]}${where ? ` on ${where}` : ""}`}
                    title="Dismiss"
                    className="shrink-0 rounded-base p-1.5 text-muted hover:bg-raised hover:text-primary"
                    onClick={() => remove.mutate(n.id)}
                  >
                    <X size={13} strokeWidth={1.75} aria-hidden />
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </PopoverContent>
    </Popover>
  );
}
