import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { audit } from "@/api/resources";
import { useIsAdmin } from "@/auth/session";
import { Button, PageTitle, TextInput } from "@/components/ui";

const STARRED = new Set(["run.confirmed", "run.applied"]);

export function AuditPage() {
  const isAdmin = useIsAdmin();
  const [action, setAction] = useState("");
  const { data } = useQuery({
    queryKey: ["audit", action],
    queryFn: () => audit.list(action ? { action } : {}),
  });
  // CSV export (admin-only endpoint). Fetch the authenticated blob, then trigger a browser download.
  const exportCsv = useMutation({
    mutationFn: () => audit.exportCsv(action ? { action } : {}),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "audit.csv";
      a.click();
      URL.revokeObjectURL(url);
    },
  });

  return (
    <div>
      <div className="mb-4 flex items-center justify-between gap-3">
        <PageTitle>Audit</PageTitle>
        <div className="flex items-center gap-2">
          <TextInput
            placeholder="filter by action (e.g. run.applied)"
            value={action}
            onChange={(e) => setAction(e.target.value)}
          />
          {isAdmin && (
            <Button onClick={() => exportCsv.mutate()} disabled={exportCsv.isPending}>
              Export CSV
            </Button>
          )}
        </div>
      </div>
      {exportCsv.isError && (
        <div className="mb-3 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {(exportCsv.error as Error).message}
        </div>
      )}
      <table className="w-full text-left font-data text-[12px]">
        <thead>
          <tr style={{ color: "var(--color-text-secondary)" }}>
            <th className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>WHEN</th>
            <th className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>ACTOR</th>
            <th className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>ACTION</th>
            <th className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>TARGET</th>
          </tr>
        </thead>
        <tbody>
          {(data ?? []).map((e) => (
            <tr
              key={e.id}
              style={{ backgroundColor: STARRED.has(e.action) ? "var(--color-bg-raised)" : undefined }}
            >
              <td className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>
                {new Date(e.created_at).toLocaleString()}
              </td>
              <td className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>
                {e.actor_email ?? e.actor_kind}
              </td>
              <td className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>{e.action}</td>
              <td className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>
                {e.target_kind ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
