import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { audit } from "@/api/resources";
import { PageTitle, TextInput } from "@/components/ui";

const STARRED = new Set(["run.confirmed", "run.applied"]);

export function AuditPage() {
  const [action, setAction] = useState("");
  const { data } = useQuery({
    queryKey: ["audit", action],
    queryFn: () => audit.list(action ? { action } : {}),
  });

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <PageTitle>Audit</PageTitle>
        <TextInput
          placeholder="filter by action (e.g. run.applied)"
          value={action}
          onChange={(e) => setAction(e.target.value)}
        />
      </div>
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
