import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { queue } from "@/api/resources";
import { PageTitle } from "@/components/ui";

const REASON: Record<string, string> = {
  active_run: "another run active on this environment",
  env_locked: "environment locked",
  no_compatible_worker: "no compatible worker",
  apply_affinity_hold: "apply affinity reservation",
};

export function QueuePage() {
  const { data } = useQuery({ queryKey: ["queue"], queryFn: queue.list, refetchInterval: 3000 });
  return (
    <div>
      <PageTitle>Queue</PageTitle>
      <table className="w-full text-left font-data text-[12px]">
        <thead>
          <tr style={{ color: "var(--color-text-secondary)" }}>
            <th className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>RUN</th>
            <th className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>STATE</th>
            <th className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>WORKER</th>
            <th className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>BLOCKED BY</th>
          </tr>
        </thead>
        <tbody>
          {(data ?? []).map((q) => (
            <tr key={q.run_id}>
              <td className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>
                <Link to={`/runs/${q.run_id}`} style={{ color: "var(--color-accent)" }}>
                  {q.run_id.slice(0, 8)}
                </Link>
              </td>
              <td className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>{q.state}</td>
              <td className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>
                {q.worker_id ? q.worker_id.slice(0, 8) : "—"}
              </td>
              <td className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>
                {q.blocking_reason ? (REASON[q.blocking_reason] ?? q.blocking_reason) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {data && data.length === 0 && (
        <p className="mt-3 text-[13px]" style={{ color: "var(--color-text-secondary)" }}>
          Queue is empty.
        </p>
      )}
    </div>
  );
}
