import type { ImportReport } from "@/api/types";
import { OneTimeSecret } from "@/ui/OneTimeSecret";
import { Banner, CopyButton } from "@/ui/primitives";
import { groupChanges, kindLabel, missingItem, skipReason, warningText } from "./importReport";

// A configuration import report (SPECS §22.3) in words: counts, the secrets left to fill first, a
// created pool's token (once), warnings, what was skipped and why, then the changes by op and kind.

function Count({ n, label }: { n: number; label: string }) {
  return (
    <div className="flex flex-col">
      <dt className="eyebrow text-muted">{label}</dt>
      <dd className="font-data text-section">{n}</dd>
    </div>
  );
}

const OP_TITLE = { created: "To create", updated: "To update", unchanged: "Unchanged" } as const;
const OP_TITLE_DONE = { created: "Created", updated: "Updated", unchanged: "Unchanged" } as const;

export function ImportReportView({ report }: { report: ImportReport }) {
  const groups = groupChanges(report.changes);
  const titles = report.dry_run ? OP_TITLE : OP_TITLE_DONE;
  const mappedEnvs = Object.keys(report.id_map.environments).length;
  return (
    <div className="flex flex-col gap-3" aria-label={report.dry_run ? "Dry run report" : "Import report"}>
      <dl className="flex flex-wrap gap-x-6 gap-y-2">
        <Count n={report.summary.created} label={report.dry_run ? "would create" : "created"} />
        <Count n={report.summary.updated} label={report.dry_run ? "would update" : "updated"} />
        <Count n={report.summary.unchanged} label="unchanged" />
        <Count n={report.summary.skipped} label="skipped" />
      </dl>

      {report.missing_secrets.length > 0 && (
        <Banner
          tone="decision"
          role="status"
          title={`${report.missing_secrets.length} secret${report.missing_secrets.length === 1 ? "" : "s"} to fill before runs flow`}
        >
          <p className="text-[12px]">
            A document never carries a secret. Set these through the stack and environment settings
            {report.dry_run ? " once imported" : ""}; new environments keep autodeploy off meanwhile.
          </p>
          <ul className="mt-1 flex flex-col gap-0.5 text-[12px]">
            {report.missing_secrets.map((m, i) => (
              <li key={i}>
                <span className="font-data break-all">{m.path}</span> — {missingItem(m)}
              </li>
            ))}
          </ul>
        </Banner>
      )}

      {report.worker_pool_tokens.map((t) => (
        <OneTimeSecret
          key={t.name}
          label={`Agent token for pool ${t.name}`}
          value={t.token}
          hint="Start the pool's agents with it."
        />
      ))}

      {report.warnings.length > 0 && (
        <Banner
          tone="info"
          title={`${report.warnings.length} warning${report.warnings.length === 1 ? "" : "s"}`}
        >
          <ul className="flex flex-col gap-0.5 text-[12px]">
            {report.warnings.map((w, i) => (
              <li key={i}>
                {w.path && <span className="font-data break-all">{w.path}</span>}
                {w.path ? " — " : ""}
                {warningText(w)}
              </li>
            ))}
          </ul>
        </Banner>
      )}

      {report.skipped.length > 0 && (
        <section aria-label="Skipped" className="flex flex-col gap-1">
          <h3 className="eyebrow text-muted">
            Skipped ({report.skipped.length}) — {report.dry_run ? "would not be imported" : "not imported"}
          </h3>
          <ul className="flex flex-col gap-0.5 text-[12px]">
            {report.skipped.map((s, i) => (
              <li key={i}>
                <span className="text-muted">{kindLabel(s.kind)}</span>{" "}
                <span className="font-data break-all">{s.path}</span> — {skipReason(s)}
              </li>
            ))}
          </ul>
        </section>
      )}

      {groups.map((g) => {
        const body = (
          <div className="flex flex-col gap-2">
            {g.kinds.map((k) => (
              <div key={k.kind} className="flex flex-col gap-0.5">
                <span className="text-[12px] text-muted">
                  {k.items.length} {kindLabel(k.kind, k.items.length)}
                </span>
                <ul className="flex flex-col gap-0.5 font-data text-[12px]">
                  {k.items.map((c) => (
                    <li key={c.path} className="break-all">
                      {c.path}
                      {c.fields && c.fields.length > 0 && (
                        <span className="text-muted"> · {c.fields.join(", ")}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        );
        return g.op === "unchanged" ? (
          <details key={g.op} className="flex flex-col gap-1">
            <summary className="eyebrow cursor-pointer text-muted">
              {titles[g.op]} ({g.count})
            </summary>
            <div className="mt-1">{body}</div>
          </details>
        ) : (
          <section key={g.op} aria-label={titles[g.op]} className="flex flex-col gap-1">
            <h3 className="eyebrow text-muted">
              {titles[g.op]} ({g.count})
            </h3>
            {body}
          </section>
        );
      })}

      {!report.dry_run && mappedEnvs > 0 && (
        <p className="flex items-center gap-1 text-[12px] text-muted">
          Managed state does not move with an import: to bring an environment&apos;s state, use an import
          session with the id map (source id → new id).
          <CopyButton value={JSON.stringify(report.id_map, null, 2)} label="Copy the id map" />
        </p>
      )}
    </div>
  );
}
