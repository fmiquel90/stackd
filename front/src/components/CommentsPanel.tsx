import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CornerDownRight, X } from "lucide-react";
import { comments } from "@/api/resources";
import type { CommentAnchor, RunComment } from "@/api/types";
import { useSession } from "@/auth/session";
import { Button, Card } from "@/components/ui";

const isRoot = (c: RunComment) => c.parent_id == null;

function AnchorChip({ anchor }: { anchor: CommentAnchor }) {
  const label =
    anchor.kind === "resource"
      ? `${anchor.address}${anchor.action ? ` (${anchor.action})` : ""}`
      : // include seq: line index is chunk-relative, so phase+seq disambiguates which block.
        `${anchor.phase}#${anchor.seq} L${anchor.line_start}${
          anchor.line_end && anchor.line_end !== anchor.line_start ? `–${anchor.line_end}` : ""
        }`;
  return (
    <div
      className="font-data mb-1 flex items-center gap-1 rounded-base px-2 py-1 text-[11px]"
      style={{ backgroundColor: "var(--color-bg-base)", border: "1px solid var(--color-border)" }}
    >
      <CornerDownRight size={12} strokeWidth={1.75} aria-hidden style={{ color: "var(--color-accent)" }} />
      <span style={{ color: "var(--color-accent)" }}>{label}</span>
      {anchor.snippet ? (
        <span style={{ color: "var(--color-text-secondary)" }}> · {anchor.snippet}</span>
      ) : null}
    </div>
  );
}

function Composer({
  runId,
  parentId,
  anchor,
  placeholder,
  onPosted,
}: {
  runId: string;
  parentId?: string;
  anchor?: CommentAnchor | null;
  placeholder: string;
  onPosted?: () => void;
}) {
  const qc = useQueryClient();
  const [body, setBody] = useState("");
  const post = useMutation({
    mutationFn: () => comments.create(runId, { body, anchor: anchor ?? null, parent_id: parentId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["run-comments", runId] });
      setBody("");
      onPosted?.();
    },
  });
  return (
    <form
      className="flex items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (body.trim()) post.mutate();
      }}
    >
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder={placeholder}
        rows={parentId ? 1 : 2}
        className="font-data flex-1 rounded-base px-2 py-1.5 text-[12px]"
        style={{
          border: "1px solid var(--color-border)",
          backgroundColor: "var(--color-bg-base)",
          color: "var(--color-text-primary)",
        }}
      />
      <Button type="submit" variant="accent" disabled={post.isPending || !body.trim()}>
        {parentId ? "Reply" : "Comment"}
      </Button>
    </form>
  );
}

function Thread({
  runId,
  root,
  replies,
  meId,
}: {
  runId: string;
  root: RunComment;
  replies: RunComment[];
  meId: string | undefined;
}) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["run-comments", runId] });
  const resolve = useMutation({
    mutationFn: () => comments.update(runId, root.id, { resolved: !root.resolved }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (cid: string) => comments.remove(runId, cid),
    onSuccess: invalidate,
  });
  const [replying, setReplying] = useState(false);

  const line = (c: RunComment) => (
    <div key={c.id} className="flex items-start justify-between gap-2">
      <div className="text-[12px]">
        <span className="font-data" style={{ color: "var(--color-text-secondary)" }}>
          {c.author_email ?? "unknown"}
          {c.edited_at ? " (edited)" : ""}:
        </span>{" "}
        <span style={{ whiteSpace: "pre-wrap" }}>{c.body}</span>
      </div>
      {c.author_user_id === meId && (
        <button
          type="button"
          aria-label="Delete comment"
          className="ui-btn shrink-0"
          style={{ color: "var(--color-state-failed)" }}
          onClick={() => remove.mutate(c.id)}
        >
          <X size={13} strokeWidth={1.75} aria-hidden />
        </button>
      )}
    </div>
  );

  return (
    <div
      className="rounded-base p-2"
      style={{ border: "1px solid var(--color-border)", opacity: root.resolved ? 0.6 : 1 }}
    >
      {root.anchor && <AnchorChip anchor={root.anchor} />}
      <div className="flex items-center justify-between gap-2">
        {root.resolved && (
          <span
            className="font-data rounded-badge px-1.5 text-[11px] uppercase"
            style={{ color: "var(--color-state-finished)", border: "1px solid var(--color-state-finished)" }}
          >
            resolved
          </span>
        )}
        <button
          type="button"
          className="ui-btn ml-auto text-[11px]"
          style={{ color: "var(--color-text-secondary)" }}
          onClick={() => resolve.mutate()}
          disabled={resolve.isPending}
        >
          {root.resolved ? "Reopen" : "Resolve"}
        </button>
      </div>
      <div className="flex flex-col gap-1">
        {line(root)}
        {replies.map((r) => (
          <div key={r.id} className="ml-3 border-l pl-2" style={{ borderColor: "var(--color-border)" }}>
            {line(r)}
          </div>
        ))}
      </div>
      {replying ? (
        <div className="mt-2">
          <Composer
            runId={runId}
            parentId={root.id}
            placeholder="Reply…"
            onPosted={() => setReplying(false)}
          />
        </div>
      ) : (
        <button
          type="button"
          className="ui-btn mt-1 text-[11px]"
          style={{ color: "var(--color-text-secondary)" }}
          onClick={() => setReplying(true)}
        >
          Reply
        </button>
      )}
    </div>
  );
}

export function CommentsPanel({
  runId,
  anchorDraft,
  onClearDraft,
}: {
  runId: string;
  anchorDraft: CommentAnchor | null;
  onClearDraft: () => void;
}) {
  const me = useSession().data;
  const { data } = useQuery({ queryKey: ["run-comments", runId], queryFn: () => comments.list(runId) });
  const all = data ?? [];
  const roots = all.filter(isRoot);
  const repliesOf = (id: string) => all.filter((c) => c.parent_id === id);

  return (
    <Card>
      <div className="mb-2 text-[13px] font-medium">Discussion</div>

      {anchorDraft && (
        <div className="mb-2">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
              Commenting on a plan selection
            </span>
            <button
              type="button"
              className="ui-btn text-[11px]"
              style={{ color: "var(--color-text-secondary)" }}
              onClick={onClearDraft}
            >
              Clear
            </button>
          </div>
          <AnchorChip anchor={anchorDraft} />
          <Composer
            runId={runId}
            anchor={anchorDraft}
            placeholder="Comment on this part of the plan…"
            onPosted={onClearDraft}
          />
        </div>
      )}

      <div className="flex flex-col gap-2">
        {roots.map((root) => (
          <Thread
            key={root.id}
            runId={runId}
            root={root}
            replies={repliesOf(root.id)}
            meId={me?.id}
          />
        ))}
        {roots.length === 0 && (
          <span className="text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            No comments yet. Select lines in the logs to comment on the plan, or start a thread below.
          </span>
        )}
      </div>

      {!anchorDraft && (
        <div className="mt-2">
          <Composer runId={runId} placeholder="Start a general thread…" />
        </div>
      )}
    </Card>
  );
}
