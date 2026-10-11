import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import {
  ArrowDown,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Download,
  Maximize2,
  Minimize2,
  X,
} from "lucide-react";
import type { LogChunk } from "@/api/types";
import { buildLogModel, type LogLine, type LogSection } from "@/lib/logModel";
import { formatDuration } from "@/lib/time";
import { Button } from "@/ui/Button";
import { Input, Kbd } from "@/ui/primitives";
import { cn } from "@/ui/cn";
import { AnsiText, stripAnsi } from "./Ansi";

// The log viewer (DESIGN §5.3): virtualized, sections per phase and hook, line anchors, follow-tail,
// search, first error, timestamps / wrap / full screen. Shortcuts apply while the viewer has focus.

type Item = { kind: "section"; section: LogSection } | { kind: "line"; line: LogLine };

export interface LogViewerProps {
  chunks: LogChunk[];
  /** The run is live: follow the tail by default. */
  live: boolean;
  /** Hook names whose check failed — their section header says so. */
  failedHooks?: string[];
  /** `phase.n` line to scroll to and highlight on mount (from `?line=`). */
  initialLine?: string | null;
  /** Called when a line number is clicked (the page writes `?line=` and copies the URL). */
  onLineLink?: (id: string) => void;
  /** Rendered under a line (inline review threads, SPECS §16). */
  renderAfterLine?: (line: LogLine) => ReactNode;
  /** Hover action on a line (e.g. comment on it). */
  lineAction?: (line: LogLine) => ReactNode;
  /** Height of the scroll area (the run page fills the viewport). */
  className?: string;
}

export function LogViewer({
  chunks,
  live,
  failedHooks = [],
  initialLine,
  onLineLink,
  renderAfterLine,
  lineAction,
  className,
}: LogViewerProps) {
  const model = useMemo(() => buildLogModel(chunks), [chunks]);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [timestamps, setTimestamps] = useState(false);
  const [wrap, setWrap] = useState(true);
  const [full, setFull] = useState(false);
  const [following, setFollowing] = useState(live && !initialLine);
  const [query, setQuery] = useState("");
  const [hit, setHit] = useState(0);
  const [target, setTarget] = useState<string | null>(initialLine ?? null);
  const [seenCount, setSeenCount] = useState(model.lines.length);
  const ref = useRef<VirtuosoHandle>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const failedSet = useMemo(() => new Set(failedHooks.map((h) => `hook:${h}`)), [failedHooks]);

  const items = useMemo(() => {
    const out: Item[] = [];
    for (const section of model.sections) {
      out.push({ kind: "section", section });
      if (!collapsed.has(section.index)) {
        for (let i = section.start; i < section.start + section.count; i++)
          out.push({ kind: "line", line: model.lines[i] });
      }
    }
    return out;
  }, [model, collapsed]);

  const indexOfLine = (id: string) => items.findIndex((it) => it.kind === "line" && it.line.id === id);

  const hits = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [] as string[];
    return model.lines.filter((l) => stripAnsi(l.msg).toLowerCase().includes(q)).map((l) => l.id);
  }, [model, query]);

  const reveal = (id: string) => {
    const line = model.lines.find((l) => l.id === id);
    if (line && collapsed.has(line.sectionIndex)) {
      setCollapsed((c) => {
        const n = new Set(c);
        n.delete(line.sectionIndex);
        return n;
      });
    }
    setFollowing(false);
    setTarget(id);
  };

  // Scroll to the target once it is in the item list (after an expand, or on mount).
  useEffect(() => {
    if (!target) return;
    const i = indexOfLine(target);
    if (i >= 0) ref.current?.scrollToIndex({ index: i, align: "center" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, items]);

  const newLines = following ? 0 : Math.max(0, model.lines.length - seenCount);

  const step = (dir: 1 | -1) => {
    if (!hits.length) return;
    const next = (hit + dir + hits.length) % hits.length;
    setHit(next);
    reveal(hits[next]);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).tagName === "INPUT" || e.metaKey || e.ctrlKey || e.altKey) {
      if ((e.metaKey || e.ctrlKey) && e.key === "f") {
        e.preventDefault();
        searchRef.current?.focus();
      }
      return;
    }
    const k = e.key;
    // Keys the viewer consumes are marked handled so global shortcuts skip them.
    if (["/", "f", "t", "w", "e", "n", "N"].includes(k)) e.preventDefault();
    if (k === "/") {
      searchRef.current?.focus();
    } else if (k === "f") setFull((v) => !v);
    else if (k === "t") setTimestamps((v) => !v);
    else if (k === "w") setWrap((v) => !v);
    else if (k === "e" && model.firstError >= 0) reveal(model.lines[model.firstError].id);
    else if (k === "n") step(1);
    else if (k === "N") step(-1);
    else if (k === "Escape" && full) setFull(false);
  };

  const download = () => {
    const text = model.lines
      .map((l) => `${l.t} ${l.phase}${l.section ? `/${l.section}` : ""} ${stripAnsi(l.msg)}`)
      .join("\n");
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "run.log";
    a.click();
    URL.revokeObjectURL(url);
  };

  const currentHit = hits[hit];

  return (
    // A scrollable region must be focusable for keyboard users (WCAG 2.1.1), and the viewer's shortcuts
    // (DESIGN §4.3) apply while focus is inside it — hence a focusable region with a key handler.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div
      role="region"
      aria-label="Run logs"
      tabIndex={0}
      onKeyDown={onKeyDown}
      className={cn(
        "flex flex-col overflow-hidden rounded-base border border-line bg-sunken",
        full ? "fixed inset-0 z-50 rounded-none border-0" : className,
      )}
    >
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-surface px-2 py-1.5">
        <Input
          ref={searchRef}
          type="search"
          value={query}
          placeholder="Search logs ( / )"
          onChange={(e) => {
            setQuery(e.target.value);
            setHit(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") step(e.shiftKey ? -1 : 1);
            if (e.key === "Escape") setQuery("");
          }}
          className="h-7 w-56"
        />
        {query && (
          <span className="flex items-center gap-1 font-data text-[12px] text-muted" aria-live="polite">
            {hits.length ? `${hit + 1}/${hits.length}` : "0 matches"}
            <button
              type="button"
              aria-label="Previous match"
              className="rounded-base p-0.5 hover:bg-raised"
              onClick={() => step(-1)}
            >
              <ChevronUp size={14} aria-hidden />
            </button>
            <button
              type="button"
              aria-label="Next match"
              className="rounded-base p-0.5 hover:bg-raised"
              onClick={() => step(1)}
            >
              <ChevronDown size={14} aria-hidden />
            </button>
          </span>
        )}
        <span className="flex-1" />
        {model.errorCount > 0 && (
          <Button size="sm" variant="secondary" onClick={() => reveal(model.lines[model.firstError].id)}>
            <span className="text-st-failed" aria-hidden>
              ✕
            </span>
            First error ({model.errorCount}) <Kbd>e</Kbd>
          </Button>
        )}
        <Button size="sm" variant="ghost" aria-pressed={timestamps} onClick={() => setTimestamps((v) => !v)}>
          Timestamps <Kbd>t</Kbd>
        </Button>
        <Button size="sm" variant="ghost" aria-pressed={wrap} onClick={() => setWrap((v) => !v)}>
          Wrap <Kbd>w</Kbd>
        </Button>
        <Button size="sm" variant="ghost" onClick={download} icon={<Download size={13} aria-hidden />}>
          Save
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setFull((v) => !v)}
          icon={full ? <Minimize2 size={13} aria-hidden /> : <Maximize2 size={13} aria-hidden />}
        >
          {full ? "Exit full screen" : "Full screen"} <Kbd>f</Kbd>
        </Button>
        {full && (
          <button
            type="button"
            aria-label="Close full screen"
            className="rounded-base p-1 hover:bg-raised"
            onClick={() => setFull(false)}
          >
            <X size={16} aria-hidden />
          </button>
        )}
      </div>

      <div className="relative min-h-0 flex-1">
        {model.lines.length === 0 ? (
          <p className="p-3 font-data text-[12px] text-muted">
            {live ? "Waiting for the first log lines…" : "No logs for this run."}
          </p>
        ) : (
          <Virtuoso
            ref={ref}
            style={{ height: "100%" }}
            data={items}
            followOutput={following ? "auto" : false}
            initialTopMostItemIndex={following ? items.length - 1 : 0}
            atBottomStateChange={(atBottom) => {
              if (atBottom) {
                setFollowing(live);
                setSeenCount(model.lines.length);
              } else if (following) {
                setFollowing(false);
                setSeenCount(model.lines.length);
              }
            }}
            computeItemKey={(_, it) => (it.kind === "section" ? `s${it.section.index}` : it.line.id)}
            itemContent={(_, it) =>
              it.kind === "section" ? (
                <SectionHeader
                  section={it.section}
                  collapsed={collapsed.has(it.section.index)}
                  failed={
                    it.section.hasError || (it.section.section ? failedSet.has(it.section.section) : false)
                  }
                  onToggle={() =>
                    setCollapsed((c) => {
                      const n = new Set(c);
                      if (n.has(it.section.index)) n.delete(it.section.index);
                      else n.add(it.section.index);
                      return n;
                    })
                  }
                />
              ) : (
                <Line
                  line={it.line}
                  wrap={wrap}
                  timestamps={timestamps}
                  targeted={it.line.id === target}
                  hit={it.line.id === currentHit}
                  query={query.trim()}
                  onLink={onLineLink}
                  action={lineAction?.(it.line)}
                  after={renderAfterLine?.(it.line)}
                />
              )
            }
          />
        )}
        {newLines > 0 && live && (
          <Button
            size="sm"
            variant="primary"
            className="absolute right-4 bottom-3"
            icon={<ArrowDown size={13} aria-hidden />}
            onClick={() => {
              setFollowing(true);
              setSeenCount(model.lines.length);
              ref.current?.scrollToIndex({ index: items.length - 1, align: "end" });
            }}
          >
            Resume following ({newLines} new line{newLines > 1 ? "s" : ""})
          </Button>
        )}
      </div>
    </div>
  );
}

function SectionHeader({
  section,
  collapsed,
  failed,
  onToggle,
}: {
  section: LogSection;
  collapsed: boolean;
  failed: boolean;
  onToggle: () => void;
}) {
  const Chevron = collapsed ? ChevronRight : ChevronDown;
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={!collapsed}
      className="flex w-full items-center gap-2 border-y border-line bg-surface px-2 py-1 text-left hover:bg-raised"
    >
      <Chevron size={14} strokeWidth={1.5} aria-hidden className="text-muted" />
      <span className="font-semibold">{section.title}</span>
      <span className="font-data text-[12px] text-muted">
        {section.count} line{section.count > 1 ? "s" : ""}
        {section.durationSeconds !== null ? ` · ${formatDuration(section.durationSeconds)}` : ""}
      </span>
      {failed && (
        <span className="eyebrow ml-auto text-st-failed">
          <span aria-hidden>✕ </span>failed
        </span>
      )}
    </button>
  );
}

function Line({
  line,
  wrap,
  timestamps,
  targeted,
  hit,
  query,
  onLink,
  action,
  after,
}: {
  line: LogLine;
  wrap: boolean;
  timestamps: boolean;
  targeted: boolean;
  hit: boolean;
  query: string;
  onLink?: (id: string) => void;
  action?: ReactNode;
  after?: ReactNode;
}) {
  const matched = query && stripAnsi(line.msg).toLowerCase().includes(query.toLowerCase());
  return (
    <div id={`L-${line.id}`}>
      <div
        className={cn(
          // 24 px rows: the line-number and comment targets meet WCAG 2.5.8 with any pointer.
          "group flex items-start font-data text-[12px] leading-6",
          targeted && "bg-raised outline outline-1 -outline-offset-1 outline-ring",
          hit && !targeted && "bg-raised",
          line.isError && "border-l-2 border-st-failed",
          !line.isError && "border-l-2 border-transparent",
        )}
      >
        <button
          type="button"
          onClick={() => onLink?.(line.id)}
          aria-label={`Link to line ${line.id}`}
          className="w-14 shrink-0 pr-2 text-right text-muted select-none hover:text-primary"
        >
          {line.n}
        </button>
        {timestamps && <span className="w-16 shrink-0 text-muted select-none">{line.t}</span>}
        <span
          className={cn(
            "min-w-0 flex-1 pr-2",
            wrap ? "break-words whitespace-pre-wrap" : "whitespace-pre",
            matched && "underline decoration-ring",
          )}
        >
          <AnsiText text={line.msg || " "} />
        </span>
        {action && (
          <span className="shrink-0 pr-1 opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 pointer-coarse:opacity-100">
            {action}
          </span>
        )}
      </div>
      {after}
    </div>
  );
}
