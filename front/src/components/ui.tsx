import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

// Minimal token-based primitives. No hard-coded colors (CLAUDE §5 / DESIGN §8).

export function Button({
  variant = "default",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "accent" }) {
  const accent = variant === "accent";
  return (
    <button
      {...props}
      className="ui-btn rounded-base px-3 py-1.5 text-[13px] font-medium disabled:opacity-50"
      style={{
        border: "1px solid var(--color-border)",
        color: accent ? "var(--color-bg-base)" : "var(--color-text-primary)",
        backgroundColor: accent ? "var(--color-accent)" : "var(--color-bg-raised)",
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
