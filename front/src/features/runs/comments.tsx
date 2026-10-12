import { useRef, useState, type KeyboardEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CornerDownRight } from "lucide-react";
import { comments, users } from "@/api/resources";
import type { CommentAnchor, MentionableUser, RunComment } from "@/api/types";
import { Button } from "@/ui/Button";
import { Badge, RelativeTime } from "@/ui/primitives";
import { cn } from "@/ui/cn";

// Plan-review threads (SPECS §16): general, resource-anchored (PlanDiff) or line-anchored (logs).
// Advisory: open threads never gate the confirm (SPECS §16.4), so their counter is neutral.

const localPart = (email: string) => email.split("@")[0];
const MENTION_RE = /(^|\s)@([\w.+-]*)$/;

export const isRoot = (c: RunComment) => c.parent_id == null;

/** Key of a log line anchor: chunk-relative index, so phase + seq + line identifies it. */
export const lineKey = (phase: string, seq: number, line: number) => `${phase}|${seq}|${line}`;

export function commentLineKey(c: RunComment): string | null {
  const a = c.anchor;
  if (!a || a.kind !== "plan_line" || a.phase == null || a.seq == null || a.line_start == null) return null;
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
    <div className="mb-1 flex items-center gap-1 rounded-base border border-line bg-sunken px-2 py-1 font-data text-[11px]">
      <CornerDownRight size={12} strokeWidth={1.75} aria-hidden className="text-muted" />
      <span>{label}</span>
      {anchor.snippet && <span className="truncate text-muted"> · {anchor.snippet}</span>}
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
  const ta = useRef<HTMLTextAreaElement>(null);
  // @mention autocomplete inserts @<local-part>, matching the server-side mention parser.
  const [query, setQuery] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const directory = useQuery({
    queryKey: ["mentionable"],
    queryFn: users.mentionable,
    staleTime: 5 * 60_000,
  });
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

  const insert = (u: MentionableUser) => {
    const caret = ta.current?.selectionStart ?? body.length;
    const m = body.slice(0, caret).match(MENTION_RE);
    if (!m) return;
    const start = caret - m[2].length - 1;
    const ins = `@${localPart(u.email)} `;
    setBody(body.slice(0, start) + ins + body.slice(caret));
    setQuery(null);
    requestAnimationFrame(() => {
      ta.current?.focus();
      ta.current?.setSelectionRange(start + ins.length, start + ins.length);
    });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && body.trim()) {
      e.preventDefault();
      post.mutate();
      return;
    }
    if (query === null || matches.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => (i + 1) % matches.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => (i - 1 + matches.length) % matches.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      insert(matches[active]);
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
          ref={ta}
          value={body}
          aria-label={placeholder}
          onChange={(e) => onType(e.target.value, e.target.selectionStart ?? e.target.value.length)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          rows={parentId ? 1 : 2}
          autoFocus={autoFocus}
          className="w-full rounded-base border border-control bg-base px-2 py-1.5 font-data text-[12px] text-primary placeholder:text-muted"
        />
        {query !== null && matches.length > 0 && (
          <ul
            role="listbox"
            aria-label="Mention"
            className="absolute bottom-full left-0 z-50 mb-1 w-64 rounded-base border border-control bg-raised p-1"
          >
            {matches.map((u, i) => (
              <li key={u.id} role="option" aria-selected={i === active}>
                <button
                  type="button"
                  // onMouseDown (not onClick) so it fires before the textarea blur.
                  onMouseDown={(e) => {
                    e.preventDefault();
                    insert(u);
                  }}
                  className={cn(
                    "flex w-full flex-col items-start rounded-base px-2 py-1 text-left text-[12px]",
                    i === active && "bg-surface",
                  )}
                >
                  <span className="font-data">@{localPart(u.email)}</span>
                  {u.display_name && <span className="text-[11px] text-muted">{u.display_name}</span>}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <Button type="submit" variant="primary" disabled={!body.trim()} loading={post.isPending}>
        {parentId ? "Reply" : "Comment"}
      </Button>
      {onCancel && (
        <Button type="button" variant="ghost" onClick={onCancel}>
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
    <div key={c.id} className="flex items-start justify-between gap-2 text-[12px]">
      <div>
        <span className="font-data text-muted">
          {c.author_email ?? "unknown"} · <RelativeTime value={c.created_at} />
          {c.edited_at ? " (edited)" : ""}
        </span>
        <div className="whitespace-pre-wrap">{c.body}</div>
      </div>
      {c.author_user_id === meId && (
        <Button size="sm" variant="ghost" onClick={() => remove.mutate(c.id)} loading={remove.isPending}>
          Delete
        </Button>
      )}
    </div>
  );

  return (
    <div
      className={cn(
        "rounded-base border bg-surface p-2",
        root.resolved ? "border-dashed border-line" : "border-line",
      )}
    >
      {!hideAnchor && root.anchor && <AnchorChip anchor={root.anchor} />}
      <div className="mb-1 flex items-center gap-2">
        {root.resolved ? <Badge glyph="✓">resolved</Badge> : <Badge>open</Badge>}
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto"
          onClick={() => resolve.mutate()}
          loading={resolve.isPending}
        >
          {root.resolved ? "Reopen" : "Resolve"}
        </Button>
      </div>
      <div className={cn("flex flex-col gap-1.5", root.resolved && "text-muted")}>
        {line(root)}
        {replies.map((r) => (
          <div key={r.id} className="ml-3 border-l border-line pl-2">
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
        <Button size="sm" variant="ghost" className="mt-1" onClick={() => setReplying(true)}>
          Reply
        </Button>
      )}
    </div>
  );
}

/** The Discussion tab: general and resource-anchored threads; line threads live inline in the logs. */
export function Discussion({
  runId,
  threads,
  meId,
}: {
  runId: string;
  threads: RunComment[];
  meId?: string;
}) {
  const general = threads.filter((c) => isRoot(c) && commentLineKey(c) === null);
  const lineThreads = threads.filter((c) => isRoot(c) && commentLineKey(c) !== null);
  const repliesOf = (id: string) => threads.filter((c) => c.parent_id === id);
  return (
    <div className="flex max-w-[860px] flex-col gap-3">
      <CommentComposer runId={runId} placeholder="Start a thread on this run… (⌘↵ to post)" />
      {general.map((root) => (
        <CommentThread key={root.id} runId={runId} root={root} replies={repliesOf(root.id)} meId={meId} />
      ))}
      {lineThreads.length > 0 && (
        <>
          <h3 className="eyebrow mt-2 text-muted">On log lines</h3>
          {lineThreads.map((root) => (
            <CommentThread key={root.id} runId={runId} root={root} replies={repliesOf(root.id)} meId={meId} />
          ))}
        </>
      )}
      {general.length + lineThreads.length === 0 && (
        <p className="text-muted">
          No discussion yet. Comment on a resource from the Plan tab, or on a line from the Logs tab.
        </p>
      )}
    </div>
  );
}
