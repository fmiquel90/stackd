import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Bell } from "lucide-react";
import { inbox } from "@/api/resources";
import { useEntityStream } from "@/api/stream";
import type { UserNotification } from "@/api/types";

const LABEL: Record<UserNotification["kind"], string> = {
  approval_request: "Awaiting your approval",
  run_finished: "Run finished",
  run_failed: "Run failed",
  comment_reply: "New reply on your thread",
  mention: "You were mentioned",
};

function describe(n: UserNotification): string {
  const env = n.context?.environment;
  const by = n.context?.by;
  if (n.kind === "approval_request" && typeof env === "string") return `${LABEL[n.kind]} · ${env}`;
  if (n.kind === "mention" && typeof by === "string") return `${LABEL[n.kind]} by ${by}`;
  return LABEL[n.kind];
}

// In-app notification center (SPECS §17). Live via the private user:<id> WS channel; the bell shows
// the unread count and the dropdown links each item to its run.
export function NotificationBell({ userId }: { userId: string }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  useEntityStream(`user:${userId}`, [["notifications"]]);
  const { data } = useQuery({ queryKey: ["notifications"], queryFn: inbox.list });
  const items = data ?? [];
  const unread = items.filter((n) => !n.read).length;

  const markRead = useMutation({
    mutationFn: () => inbox.markRead(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && unread > 0) markRead.mutate(); // opening clears the unread badge
  };

  const go = (n: UserNotification) => {
    setOpen(false);
    if (n.run_id) navigate(`/runs/${n.run_id}`);
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={toggle}
        aria-label={`Notifications${unread ? ` (${unread} unread)` : ""}`}
        className="ui-btn relative flex items-center"
        style={{ color: "var(--color-text-secondary)" }}
      >
        <Bell size={16} strokeWidth={1.75} aria-hidden />
        {unread > 0 && (
          <span
            className="font-data absolute -top-1 -right-1 rounded-badge px-1 text-[10px]"
            style={{ backgroundColor: "var(--color-accent)", color: "var(--color-bg-base)" }}
          >
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div
          className="absolute right-0 z-50 mt-2 w-80 rounded-base p-2"
          style={{
            backgroundColor: "var(--color-bg-surface)",
            border: "1px solid var(--color-border)",
            boxShadow: "0 8px 24px var(--color-overlay)",
          }}
        >
          <div className="mb-1 px-1 text-[12px] font-medium">Notifications</div>
          <div className="flex max-h-[360px] flex-col gap-0.5 overflow-auto">
            {items.map((n) => (
              <button
                key={n.id}
                type="button"
                onClick={() => go(n)}
                className="ui-btn flex flex-col items-start rounded-base px-2 py-1.5 text-left"
                style={{ backgroundColor: n.read ? "transparent" : "var(--color-bg-raised)" }}
              >
                <span className="text-[12px]">{describe(n)}</span>
                <span className="font-data text-[11px]" style={{ color: "var(--color-text-secondary)" }}>
                  {new Date(n.created_at).toLocaleString()}
                </span>
              </button>
            ))}
            {items.length === 0 && (
              <span className="px-2 py-3 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
                Nothing yet.
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
