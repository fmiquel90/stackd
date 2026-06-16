import { useState } from "react";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";
import { Check, type LucideIcon, Trash2 } from "lucide-react";

// Minimal token-based primitives. No hard-coded colors (CLAUDE §5 / DESIGN §8).

// A config list item — a bordered tile on the base surface, used uniformly across the
// Variables / Hooks / Notifications / Secret-sources panels. `dimmed` recedes an inactive item.
export function ItemTile({ dimmed, children }: { dimmed?: boolean; children: ReactNode }) {
  return (
    <div
      className="rounded-base p-3"
      style={{
        backgroundColor: "var(--color-bg-base)",
        border: "1px solid var(--color-border)",
        opacity: dimmed ? 0.55 : 1,
      }}
    >
      {children}
    </div>
  );
}

// A small pill. `color` (a token) → colored pill (border+text); otherwise neutral (muted text,
// line border). Color is paired with the label, never the only signal (DESIGN §7).
export function Badge({
  children,
  color,
  icon: Icon,
}: {
  children: ReactNode;
  color?: string;
  icon?: LucideIcon;
}) {
  const text = color ?? "var(--color-text-secondary)";
  const border = color ?? "var(--color-border)";
  return (
    <span
      className="font-data inline-flex items-center gap-1 rounded-badge px-1.5 py-0.5 text-[11px]"
      style={{ color: text, border: `1px solid ${border}` }}
    >
      {Icon && <Icon size={11} strokeWidth={1.75} aria-hidden />}
      {children}
    </span>
  );
}

// The uniform delete affordance (Trash2, danger token) for config list items.
export function DeleteButton({ label, onClick, disabled }: { label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      aria-label={label}
      className="ui-btn rounded-base px-1.5 py-1"
      onClick={onClick}
      disabled={disabled}
      style={{ color: "var(--color-state-failed)" }}
    >
      <Trash2 size={14} strokeWidth={1.75} aria-hidden />
    </button>
  );
}

// `accent` = brand violet (primary actions). `decision` = amber, reserved for the human-decision
// moment (Confirm/apply) so it stands apart from the violet chrome (DESIGN §3.2 / invariant #4).
// Token-driven checkbox: a real <input> (kept for a11y, visually hidden) + a styled box that fills
// with the brand accent when checked. Label is mono, matching Field labels (DESIGN §2.1).
export function Checkbox({
  checked,
  onChange,
  label,
  disabled,
  className = "",
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: ReactNode;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <label
      className={`font-data inline-flex select-none items-center gap-2 text-[13px] ${disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"} ${className}`}
    >
      <input
        type="checkbox"
        className="peer sr-only"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span
        aria-hidden
        className="flex h-4 w-4 shrink-0 items-center justify-center transition-colors peer-focus-visible:[outline:2px_solid_var(--color-accent)] peer-focus-visible:[outline-offset:2px]"
        style={{
          borderRadius: 4,
          border: `1px solid ${checked ? "var(--color-accent)" : "var(--color-border)"}`,
          backgroundColor: checked ? "var(--color-accent)" : "transparent",
        }}
      >
        {checked && <Check size={12} strokeWidth={3} style={{ color: "var(--color-bg-base)" }} aria-hidden />}
      </span>
      {label}
    </label>
  );
}

// Controlled key=value map editor (stack/env labels, variable-set selector). Emits the full map on
// every change; the parent owns persistence. Keys are set on add (re-add to rename).
export function LabelsEditor({
  value,
  onChange,
  keyPlaceholder = "key",
  valuePlaceholder = "value",
}: {
  value: Record<string, unknown> | null;
  onChange: (next: Record<string, string>) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
}) {
  const entries = Object.entries(value ?? {}).map(([k, v]) => [k, String(v)] as [string, string]);
  const [k, setK] = useState("");
  const [v, setV] = useState("");
  const emit = (next: [string, string][]) => onChange(Object.fromEntries(next));
  return (
    <div className="flex flex-col gap-2">
      {entries.map(([ek, ev]) => (
        <div key={ek} className="flex items-center gap-2">
          <span className="font-data text-[12px]" style={{ minWidth: 120 }}>
            {ek}
          </span>
          <TextInput
            value={ev}
            onChange={(e) => emit(entries.map((p) => (p[0] === ek ? [ek, e.target.value] : p)))}
          />
          <DeleteButton label={`Remove ${ek}`} onClick={() => emit(entries.filter((p) => p[0] !== ek))} />
        </div>
      ))}
      <div className="flex items-end gap-2">
        <TextInput value={k} placeholder={keyPlaceholder} onChange={(e) => setK(e.target.value)} />
        <TextInput value={v} placeholder={valuePlaceholder} onChange={(e) => setV(e.target.value)} />
        <Button
          type="button"
          disabled={!k}
          onClick={() => {
            emit([...entries.filter((p) => p[0] !== k), [k, v]]);
            setK("");
            setV("");
          }}
        >
          Add
        </Button>
      </div>
    </div>
  );
}

export function Button({
  variant = "default",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "accent" | "decision" }) {
  const filled = variant === "accent" || variant === "decision";
  const bg =
    variant === "accent"
      ? "var(--color-accent)"
      : variant === "decision"
        ? "var(--color-decision)"
        : "var(--color-bg-raised)";
  return (
    <button
      {...props}
      className="ui-btn rounded-base px-3 py-1.5 text-[13px] font-medium disabled:opacity-50"
      style={{
        border: "1px solid var(--color-border)",
        color: filled ? "var(--color-bg-base)" : "var(--color-text-primary)",
        backgroundColor: bg,
      }}
    />
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        {label}
      </span>
      {children}
    </label>
  );
}

const controlStyle = {
  border: "1px solid var(--color-border)",
  backgroundColor: "var(--color-bg-base)",
  color: "var(--color-text-primary)",
};

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className="font-data rounded-base px-2 py-1.5 text-[13px]" style={controlStyle} />;
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className="font-data rounded-base px-2 py-1.5 text-[13px]" style={controlStyle} />;
}

export function Card({ children }: { children: ReactNode }) {
  return (
    <div
      className="rounded-base p-4"
      style={{ backgroundColor: "var(--color-bg-surface)", border: "1px solid var(--color-border)" }}
    >
      {children}
    </div>
  );
}

export function PageTitle({ children }: { children: ReactNode }) {
  return <h1 className="mb-4 text-[20px] font-semibold tracking-[-0.01em]">{children}</h1>;
}

// Horizontal tab bar (underline = active). Used for both the stack page's top-level tabs and its
// settings sub-tabs; color is paired with the underline + aria-selected so it's not the sole signal.
export function Tabs<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: { key: T; label: string }[];
  active: T;
  onChange: (key: T) => void;
}) {
  return (
    <div className="flex gap-1" style={{ borderBottom: "1px solid var(--color-border)" }} role="tablist">
      {tabs.map((t) => {
        const isActive = t.key === active;
        return (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={isActive}
            onClick={() => onChange(t.key)}
            className="ui-btn px-3 py-2 text-[13px] font-medium"
            style={{
              color: isActive ? "var(--color-text-primary)" : "var(--color-text-secondary)",
              borderBottom: isActive ? "2px solid var(--color-accent)" : "2px solid transparent",
              marginBottom: -1,
              backgroundColor: "transparent",
            }}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}
