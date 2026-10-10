import {
  forwardRef,
  useState,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TdHTMLAttributes,
  type ThHTMLAttributes,
} from "react";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import { Check, Copy, TriangleAlert, Info, X, CircleSlash } from "lucide-react";
import { cn } from "./cn";
import { Tooltip } from "./overlays";
import { isoLocal, relative } from "@/lib/time";

// Small token-based primitives (DESIGN §8 inventory). No inline colour, no opacity for meaning.

/* ---------- Form controls ---------- */

const CONTROL =
  "h-8 rounded-base border border-control bg-base px-2 font-data text-ui text-primary placeholder:text-muted disabled:cursor-not-allowed disabled:border-dashed disabled:text-muted";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input(
  { className, ...rest },
  ref,
) {
  return <input ref={ref} className={cn(CONTROL, className)} {...rest} />;
});

export const NativeSelect = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function NativeSelect({ className, ...rest }, ref) {
    return <select ref={ref} className={cn(CONTROL, "pr-7", className)} {...rest} />;
  },
);

export function Field({ label, help, children }: { label: string; help?: ReactNode; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="eyebrow text-muted">{label}</span>
      {children}
      {help && <span className="text-[12px] text-muted">{help}</span>}
    </label>
  );
}

/* ---------- Tabs (Radix; the caller binds `value` to the URL) ---------- */

export const Tabs = TabsPrimitive.Root;
export const TabsContent = TabsPrimitive.Content;

export function TabsList({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <TabsPrimitive.List className={cn("flex gap-1 overflow-x-auto border-b border-line", className)}>
      {children}
    </TabsPrimitive.List>
  );
}

export function TabsTrigger({ value, children }: { value: string; children: ReactNode }) {
  return (
    <TabsPrimitive.Trigger
      value={value}
      className={cn(
        "-mb-px flex shrink-0 items-center gap-1.5 border-b-2 border-transparent px-3 py-2 text-ui font-medium whitespace-nowrap text-muted",
        "hover:text-primary data-[state=active]:border-ring data-[state=active]:text-primary",
      )}
    >
      {children}
    </TabsPrimitive.Trigger>
  );
}

/* ---------- Table ---------- */

export function Table({ className, ...rest }: HTMLAttributes<HTMLTableElement>) {
  return <table className={cn("w-full border-collapse text-left", className)} {...rest} />;
}

export function Th({ className, ...rest }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cn(
        "eyebrow sticky top-0 border-b border-line bg-surface px-2.5 py-1.5 font-medium text-muted",
        className,
      )}
      {...rest}
    />
  );
}

export function Td({ className, ...rest }: TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn("border-b border-line px-2.5 py-1.5 align-middle", className)} {...rest} />;
}

/* ---------- Badges ---------- */

export type Tone = "neutral" | "queued" | "running" | "decision" | "finished" | "failed" | "mock";

const TONE_TEXT: Record<Tone, string> = {
  neutral: "text-primary border-control",
  queued: "text-st-queued border-st-queued",
  running: "text-st-running border-st-running",
  decision: "text-st-decision border-st-decision",
  finished: "text-st-finished border-st-finished",
  failed: "text-st-failed border-st-failed",
  mock: "text-mocked border-mocked",
};

const TONE_SOLID: Record<Tone, string> = {
  neutral: "bg-primary text-onfill border-primary",
  queued: "bg-st-queued text-onfill border-st-queued",
  running: "bg-st-running text-onfill border-st-running",
  decision: "bg-st-decision text-onfill border-st-decision",
  finished: "bg-st-finished text-onfill border-st-finished",
  failed: "bg-st-failed text-onfill border-st-failed",
  mock: "bg-mocked text-onfill border-mocked",
};

/** Outlined (default) or solid badge; always a label, optionally a glyph (DESIGN §3.2). */
export function Badge({
  tone = "neutral",
  solid,
  glyph,
  children,
  className,
  title,
}: {
  tone?: Tone;
  solid?: boolean;
  glyph?: ReactNode;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "eyebrow inline-flex h-5 shrink-0 items-center gap-1 rounded-badge border px-1.5 whitespace-nowrap",
        solid ? TONE_SOLID[tone] : TONE_TEXT[tone],
        className,
      )}
    >
      {glyph && (
        <span aria-hidden className="font-data text-[12px] leading-none">
          {glyph}
        </span>
      )}
      {children}
    </span>
  );
}

/* ---------- Banner ---------- */

export type BannerTone = "info" | "decision" | "failed" | "mock";

const BANNER: Record<BannerTone, { cls: string; icon: ReactNode }> = {
  info: {
    cls: "bg-surface border-control",
    icon: <Info size={16} strokeWidth={1.5} className="text-muted" aria-hidden />,
  },
  decision: {
    cls: "bg-tint-decision border-st-decision",
    icon: <TriangleAlert size={16} strokeWidth={1.5} className="text-st-decision" aria-hidden />,
  },
  failed: {
    cls: "bg-tint-failed border-st-failed",
    icon: <X size={16} strokeWidth={1.75} className="text-st-failed" aria-hidden />,
  },
  mock: {
    cls: "bg-tint-mock border-mocked",
    icon: <CircleSlash size={16} strokeWidth={1.5} className="text-mocked" aria-hidden />,
  },
};

export function Banner({
  tone = "info",
  title,
  children,
  action,
  icon,
  role,
}: {
  tone?: BannerTone;
  title: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
  role?: "status" | "alert";
}) {
  const b = BANNER[tone];
  return (
    <div role={role} className={cn("flex items-start gap-3 rounded-base border px-3 py-2", b.cls)}>
      <span className="mt-0.5 shrink-0">{icon ?? b.icon}</span>
      <div className="min-w-0 flex-1">
        <div className="font-semibold">{title}</div>
        {children && <div className="mt-0.5 text-primary">{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

/* ---------- Data bits ---------- */

export function Kbd({ children }: { children: ReactNode }) {
  return (
    // Inherits the surrounding text colour so it stays legible on filled buttons (amber, red, primary).
    // Visual hint only: the control carries the shortcut in aria-keyshortcuts (not in its name).
    <kbd
      aria-hidden
      className="inline-flex h-5 min-w-5 items-center justify-center rounded-badge border border-current px-1 font-data text-[11px] font-normal"
    >
      {children}
    </kbd>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <span aria-hidden className={cn("block animate-pulse rounded-base bg-raised", className)} />;
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-start gap-2 rounded-base border border-dashed border-control px-4 py-6">
      <div className="text-section font-semibold">{title}</div>
      {children && <div className="text-muted">{children}</div>}
      {action}
    </div>
  );
}

/** Copy-to-clipboard icon button (non-destructive tool: icon allowed, with aria-label + tooltip). */
export function CopyButton({ value, label }: { value: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <Tooltip content={done ? "Copied" : label}>
      <button
        type="button"
        aria-label={label}
        className="inline-flex h-6 w-6 items-center justify-center rounded-base text-muted hover:bg-raised hover:text-primary"
        onClick={() => {
          void navigator.clipboard?.writeText(value).then(() => {
            setDone(true);
            setTimeout(() => setDone(false), 1200);
          });
        }}
      >
        {done ? (
          <Check size={13} strokeWidth={1.75} aria-hidden />
        ) : (
          <Copy size={13} strokeWidth={1.5} aria-hidden />
        )}
      </button>
    </Tooltip>
  );
}

/** A commit SHA, short and mono, full value on hover and copyable. */
export function Sha({ value, copy }: { value: string | null; copy?: boolean }) {
  if (!value) return <span className="font-data text-muted">—</span>;
  return (
    <span className="inline-flex items-center gap-0.5">
      <span className="font-data" title={value}>
        {value.slice(0, 7)}
      </span>
      {copy && <CopyButton value={value} label="Copy commit SHA" />}
    </span>
  );
}

/** Relative time in mono, the absolute ISO form on hover (DESIGN §7). */
export function RelativeTime({ value, className }: { value: string | null; className?: string }) {
  if (!value) return <span className="font-data text-muted">—</span>;
  return (
    <time dateTime={value} title={isoLocal(value)} className={cn("font-data", className)}>
      {relative(value)}
    </time>
  );
}
