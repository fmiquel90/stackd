import { KeyRound } from "lucide-react";
import { Banner, CopyButton, Input } from "./primitives";

// A credential the API returns exactly once (an API token, a worker pool token — DESIGN §5.9,
// §5.10): a read-only field to select or copy, and a plain "you won't see it again". The caller
// keeps the value in component state only and drops it when the surface closes: never a query
// cache, never browser storage.

export function OneTimeSecret({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Banner
      tone="decision"
      role="status"
      icon={<KeyRound size={16} strokeWidth={1.5} className="text-st-decision" aria-hidden />}
      title={`${label} — copy it now: you won't see it again.`}
    >
      <span className="mt-1 flex items-center gap-1">
        <Input
          readOnly
          aria-label={label}
          value={value}
          spellCheck={false}
          autoComplete="off"
          className="min-w-0 flex-1 bg-sunken"
          onFocus={(e) => e.currentTarget.select()}
        />
        <CopyButton value={value} label={`Copy ${label.toLowerCase()}`} />
      </span>
      <span className="mt-1 block text-[12px] text-muted">
        Stackd stores only its hash: once this is closed, it is gone.{hint ? ` ${hint}` : ""}
      </span>
    </Banner>
  );
}
