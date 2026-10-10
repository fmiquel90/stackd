import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Loader } from "lucide-react";
import { cn } from "./cn";

// Buttons (DESIGN §2.1, §3.1, §7). The chrome is neutral: `primary` is a text-primary fill; amber
// (`decision`) exists for exactly one job — Confirm — and red (`danger`) for typed-name destructive
// submits. Disabled never uses opacity: dashed border + muted text, and the caller shows the reason.

export type ButtonVariant = "primary" | "secondary" | "ghost" | "decision" | "danger";
export type ButtonSize = "sm" | "md" | "touch";

const VARIANT: Record<ButtonVariant, string> = {
  primary: "bg-primary text-onfill border border-primary hover:bg-primary/90",
  secondary: "bg-surface text-primary border border-control hover:bg-raised",
  ghost: "bg-transparent text-primary border border-transparent hover:bg-raised",
  decision: "bg-st-decision text-onfill border border-st-decision hover:bg-st-decision/90 font-semibold",
  danger: "bg-st-failed text-onfill border border-st-failed hover:bg-st-failed/90 font-semibold",
};

const DISABLED =
  "disabled:cursor-not-allowed disabled:bg-raised disabled:text-muted disabled:border-dashed disabled:border-control disabled:hover:bg-raised";

const SIZE: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-[12px] gap-1.5",
  md: "h-8 px-3 text-ui gap-2",
  touch: "h-11 px-4 text-ui gap-2", // 44 px: the on-call mobile path (DESIGN §7)
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Async action in flight: spinner + disabled, label kept (distinct from a permission refusal). */
  loading?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "secondary",
    size = "md",
    loading,
    icon,
    className,
    children,
    disabled,
    type = "button",
    ...rest
  },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-base font-medium whitespace-nowrap transition-colors",
        VARIANT[variant],
        DISABLED,
        SIZE[size],
        className,
      )}
      {...rest}
    >
      {loading ? <Loader size={14} strokeWidth={1.75} className="animate-spin" aria-hidden /> : icon}
      {children}
    </button>
  );
});
