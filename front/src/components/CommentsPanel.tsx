import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CornerDownRight, X } from "lucide-react";
import { comments, users } from "@/api/resources";
import type { CommentAnchor, MentionableUser, RunComment } from "@/api/types";
import { useSession } from "@/auth/session";
import { Button, Card } from "@/components/ui";

const localPart = (email: string) => email.split("@")[0];
// The token immediately before the caret, if the user is typing an @mention (else null).
const MENTION_RE = /(^|\s)@([\w.+-]*)$/;

export const isRoot = (c: RunComment) => c.parent_id == null;

// Stable key for a plan-line anchor: line index is chunk-relative, so phase+seq+line identifies it.
export const lineKey = (phase: string, seq: number, line: number) => `${phase}|${seq}|${line}`;

/** The line key a root comment is anchored to, or null if it's general / resource-anchored. */
export function commentLineKey(c: RunComment): string | null {
  const a = c.anchor;
  if (!a || a.kind !== "plan_line" || a.phase == null || a.seq == null || a.line_start == null)
    return null;
  return lineKey(a.phase, a.seq, a.line_start);
}

function AnchorChip({ anchor }: { anchor: CommentAnchor }) {
  const label =
    anchor.kind === "resource"
      ? `${anchor.address}${anchor.action ? ` (${anchor.action})` : ""}`
      : `${anchor.phase}#${anchor.seq} L${anchor.line_start}${
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

export function CommentComposer({
  runId,
  parentId,
  anchor,
  placeholder,
  autoFocus,
  onPosted,
  onCancel,
}: {
  runId: string;
  parentId?: string;
  anchor?: CommentAnchor | null;
  placeholder: string;
  autoFocus?: boolean;
  onPosted?: () => void;
  onCancel?: () => void;
}) {
  const qc = useQueryClient();
  const [body, setBody] = useState("");
  const taRef = useRef<HTMLTextAreaElement>(null);
  // @mention autocomplete: `query` is the active token (null = menu closed); insert @<local-part>
  // so it matches the server-side mention parser (email local-part).
  const [query, setQuery] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const directory = useQuery({ queryKey: ["mentionable"], queryFn: users.mentionable });
  const post = useMutation({
    mutationFn: () => comments.create(runId, { body, anchor: anchor ?? null, parent_id: parentId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["run-comments", runId] });
      setBody("");
      onPosted?.();
    },
  });

  const matches: MentionableUser[] =
    query === null
      ? []
      : (directory.data ?? [])
          .filter(
            (u) =>
              localPart(u.email).toLowerCase().startsWith(query) ||
              (u.display_name?.toLowerCase().includes(query) ?? false),
          )
          .slice(0, 6);

  const onType = (value: string, caret: number) => {
    setBody(value);
    const m = value.slice(0, caret).match(MENTION_RE);
    setQuery(m ? m[2].toLowerCase() : null);
    setActive(0);
  };

  const insertMention = (u: MentionableUser) => {
    const ta = taRef.current;
    const caret = ta?.selectionStart ?? body.length;
    const m = body.slice(0, caret).match(MENTION_RE);
    if (!m) return;
    const start = caret - m[2].length - 1; // index of the '@'
    const ins = `@${localPart(u.email)} `;
    const next = body.slice(0, start) + ins + body.slice(caret);
    setBody(next);
    setQuery(null);
    requestAnimationFrame(() => {
      ta?.focus();
      const pos = start + ins.length;
      ta?.setSelectionRange(pos, pos);
    });
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (query === null || matches.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => (i + 1) % matches.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => (i - 1 + matches.length) % matches.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      insertMention(matches[active]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setQuery(null);
    }
  };

  return (
    <form
      className="flex items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (body.trim()) post.mutate();
      }}
    >
      <div className="relative flex-1">
        <textarea
          ref={taRef}
          value={body}
          onChange={(e) => onType(e.target.value, e.target.selectionStart ?? e.target.value.length)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          rows={parentId ? 1 : 2}
          autoFocus={autoFocus}
          className="font-data w-full rounded-base px-2 py-1.5 text-[12px]"
          style={{
            border: "1px solid var(--color-border)",
            backgroundColor: "var(--color-bg-base)",
            color: "var(--color-text-primary)",
          }}
        />
        {query !== null && matches.length > 0 && (
          <div
            className="absolute bottom-full left-0 z-50 mb-1 w-64 rounded-base p-1"
            style={{
              backgroundColor: "var(--color-bg-surface)",
              border: "1px solid var(--color-border)",
              boxShadow: "0 8px 24px var(--color-overlay)",
            }}
          >
            {matches.map((u, i) => (
              <button
                key={u.id}
                type="button"
                // onMouseDown (not onClick) so it fires before the textarea blur.
                onMouseDown={(e) => {
                  e.preventDefault();
                  insertMention(u);
                }}
                className="ui-btn flex w-full flex-col items-start rounded-base px-2 py-1 text-left text-[12px]"
                style={{ backgroundColor: i === active ? "var(--color-bg-raised)" : "transparent" }}
              >
                <span>@{localPart(u.email)}</span>
                {u.display_name && (
                  <span className="text-[11px]" style={{ color: "var(--color-text-secondary)" }}>
                    {u.display_name}
                  </span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
      <Button type="submit" variant="accent" disabled={post.isPending || !body.trim()}>
        {parentId ? "Reply" : "Comment"}
      </Button>
      {onCancel && (
        <Button type="button" onClick={onCancel}>
          Cancel
        </Button>
      )}
    </form>
  );
}

export function CommentThread({
  runId,
  root,
  replies,
  meId,
  hideAnchor,
}: {
  runId: string;
  root: RunComment;
  replies: RunComment[];
  meId: string | undefined;
  hideAnchor?: boolean;
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
      style={{
        border: "1px solid var(--color-border)",
        backgroundColor: "var(--color-bg-surface)",
        opacity: root.resolved ? 0.6 : 1,
      }}
    >
      {!hideAnchor && root.anchor && <AnchorChip anchor={root.anchor} />}
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
          <CommentComposer
            runId={runId}
            parentId={root.id}
            placeholder="Reply…"
            autoFocus
            onPosted={() => setReplying(false)}
            onCancel={() => setReplying(false)}
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

// General (unanchored or resource-anchored) discussion. Plan-line threads render inline in the log
// viewer (RunPage) for a progressive, line-level review; this panel holds the rest.
export function CommentsPanel({ runId }: { runId: string }) {
  const me = useSession().data;
  const { data } = useQuery({ queryKey: ["run-comments", runId], queryFn: () => comments.list(runId) });
  const all = data ?? [];
  const general = all.filter((c) => isRoot(c) && commentLineKey(c) === null);
  const repliesOf = (id: string) => all.filter((c) => c.parent_id === id);

  return (
    <Card>
      <div className="mb-2 text-[13px] font-medium">Discussion</div>
      <div className="flex flex-col gap-2">
        {general.map((root) => (
          <CommentThread key={root.id} runId={runId} root={root} replies={repliesOf(root.id)} meId={me?.id} />
        ))}
        {general.length === 0 && (
          <span className="text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            No general comments. Hover a line in the logs to comment on the plan itself.
          </span>
        )}
      </div>
      <div className="mt-2">
        <CommentComposer runId={runId} placeholder="Start a general thread…" />
      </div>
    </Card>
  );
}
