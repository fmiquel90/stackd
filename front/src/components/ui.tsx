import { useState } from "react";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";
import { Check, type LucideIcon, Trash2 } from "lucide-react";
import { Button as UiButton, type ButtonVariant } from "@/ui/Button";
import { Popover, PopoverClose, PopoverContent, PopoverTrigger } from "@/ui/overlays";
import { Input, NativeSelect } from "@/ui/primitives";
import { cn } from "@/ui/cn";

// Form and layout primitives of the configuration panels, on the tokens (no inline colour, DESIGN §8).
// New screens use src/ui directly; these keep the panels' API stable.

/** A config list item: a bordered row with metadata badges, Edit and Delete (DESIGN §5.7). */
export function ItemTile({ dimmed, children }: { dimmed?: boolean; children: ReactNode }) {
  return (
    <div
      className={cn(
        "rounded-base border bg-base p-3",
        dimmed ? "border-dashed border-line text-muted" : "border-line",
      )}
    >
      {children}
    </div>
  );
}

/** Small label. `color` is a CSS token var naming a state — mapped onto its utility. */
export function Badge({
  children,
  color,
  icon: Icon,
}: {
  children: ReactNode;
  color?: string;
  icon?: LucideIcon;
}) {
  const tone = color?.includes("failed")
    ? "text-st-failed border-st-failed"
    : color?.includes("finished")
      ? "text-st-finished border-st-finished"
      : color?.includes("unconfirmed") || color?.includes("decision")
        ? "text-st-decision border-st-decision"
        : color?.includes("mock")
          ? "text-mocked border-mocked"
          : color?.includes("running")
            ? "text-st-running border-st-running"
            : "text-muted border-control";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-badge border px-1.5 py-0.5 font-data text-[11px]",
        tone,
      )}
    >
      {Icon && <Icon size={11} strokeWidth={1.75} aria-hidden />}
      {children}
    </span>
  );
}

/**
 * Delete with a confirmation anchored to the button — never one click, never an icon alone
 * (DESIGN §5.7, §9). `label` names the target ("Delete hook tfsec").
 */
export function DeleteButton({
  label,
  onClick,
  disabled,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <UiButton
          size="sm"
          variant="ghost"
          disabled={disabled}
          icon={<Trash2 size={13} strokeWidth={1.75} aria-hidden />}
        >
          Delete
          <span className="sr-only"> — {label}</span>
        </UiButton>
      </PopoverTrigger>
      <PopoverContent align="end" className="flex w-72 flex-col gap-2">
        <p className="text-[12px]">{label}? This cannot be undone.</p>
        <div className="flex justify-end gap-2">
          <PopoverClose asChild>
            <UiButton size="sm" variant="ghost">
              Keep
            </UiButton>
          </PopoverClose>
          <PopoverClose asChild>
            <UiButton size="sm" variant="danger" onClick={onClick}>
              {label}
            </UiButton>
          </PopoverClose>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** Checkbox: a real input (a11y) with a token-styled box; mono label. */
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
      className={cn(
        "inline-flex items-center gap-2 font-data text-[13px] select-none",
        disabled ? "cursor-not-allowed text-muted" : "cursor-pointer",
        className,
      )}
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
        className={cn(
          "flex h-4 w-4 shrink-0 items-center justify-center rounded-base border peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-ring",
          checked ? "border-primary bg-primary text-onfill" : "border-control bg-transparent",
          disabled && "border-dashed",
        )}
      >
        {checked && <Check size={12} strokeWidth={3} aria-hidden />}
      </span>
      {label}
    </label>
  );
}

/** Controlled key=value map editor (labels, selectors). Emits the full map on every change. */
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
          <span className="min-w-[120px] font-data text-[12px]">{ek}</span>
          <TextInput
            aria-label={`Value of ${ek}`}
            value={ev}
            onChange={(e) => emit(entries.map((p) => (p[0] === ek ? [ek, e.target.value] : p)))}
          />
          <UiButton size="sm" variant="ghost" onClick={() => emit(entries.filter((p) => p[0] !== ek))}>
            Remove <span className="sr-only">{ek}</span>
          </UiButton>
        </div>
      ))}
      <div className="flex items-end gap-2">
        <TextInput
          aria-label="New key"
          value={k}
          placeholder={keyPlaceholder}
          onChange={(e) => setK(e.target.value)}
        />
        <TextInput
          aria-label="New value"
          value={v}
          placeholder={valuePlaceholder}
          onChange={(e) => setV(e.target.value)}
        />
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

/** Legacy variants: `accent` is now the neutral primary (DESIGN §2.1); `decision` stays amber. */
export function Button({
  variant = "default",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "accent" | "decision" }) {
  const map: Record<string, ButtonVariant> = {
    default: "secondary",
    accent: "primary",
    decision: "decision",
  };
  return <UiButton variant={map[variant]} {...props} />;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="eyebrow text-muted">{label}</span>
      {children}
    </label>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <Input {...props} />;
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <NativeSelect {...props} />;
}

export function Card({ children }: { children: ReactNode }) {
  return <div className="rounded-base border border-line bg-surface p-4">{children}</div>;
}

export function PageTitle({ children }: { children: ReactNode }) {
  return <h1 className="mb-4 text-title font-semibold">{children}</h1>;
}

/** Horizontal tab bar (underline = active; aria-selected carries it too). */
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
    <div className="flex gap-1 overflow-x-auto border-b border-line" role="tablist">
      {tabs.map((t) => {
        const isActive = t.key === active;
        return (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={isActive}
            onClick={() => onChange(t.key)}
            className={cn(
              "-mb-px shrink-0 border-b-2 px-3 py-2 text-ui font-medium whitespace-nowrap",
              isActive ? "border-ring text-primary" : "border-transparent text-muted hover:text-primary",
            )}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}
