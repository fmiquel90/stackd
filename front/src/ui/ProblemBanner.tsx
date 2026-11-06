import { ApiError, type ProblemFieldError } from "@/api/client";
import { Banner } from "./primitives";

// An API refusal (RFC 9457 problem+json) in words: its detail, and for a 422 the list of fields at
// fault — where, and what is wrong. Never the offending input (it may be a secret).

/** Where a 422 field error points, in the payload's terms ("stacks › 0 › environments › 1 › tier"). */
export function formatLoc(loc: ProblemFieldError["loc"]): string {
  const parts = loc[0] === "body" || loc[0] === "query" ? loc.slice(1) : loc;
  return parts.length ? parts.join(" › ") : "request";
}

export function ProblemBanner({ error, title }: { error: unknown; title?: string }) {
  const api = error instanceof ApiError ? error : null;
  const heading =
    title ?? (api?.status === 403 ? "Not allowed" : api?.status === 422 ? "Invalid" : "The API refused");
  return (
    <Banner tone="failed" role="alert" title={heading}>
      <span>{api ? (api.detail ?? api.title) : String((error as Error)?.message ?? error)}</span>
      {api?.errors && api.errors.length > 0 && (
        <ul className="mt-1 flex flex-col gap-0.5 font-data text-[12px]">
          {api.errors.map((e, i) => (
            <li key={i}>
              <span className="text-muted">{formatLoc(e.loc)}</span> — {e.msg}
            </li>
          ))}
        </ul>
      )}
    </Banner>
  );
}
