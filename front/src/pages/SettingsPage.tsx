import { useQuery } from "@tanstack/react-query";
import { observability } from "@/api/resources";
import { Card, PageTitle } from "@/components/ui";

// Settings is intentionally light at MVP: users/roles admin, webhooks and cloud-integration config
// live on their own surfaces. This page surfaces deploy info; richer settings come with RBAC (Phase 7).
export function SettingsPage() {
  const { data } = useQuery({ queryKey: ["health"], queryFn: observability.health });
  return (
    <div className="flex flex-col gap-4">
      <PageTitle>Settings</PageTitle>
      <Card>
        <div className="mb-2 text-[13px] font-medium">Deployment</div>
        <table className="font-data text-[12px]">
          <tbody>
            <tr>
              <td className="py-1 pr-6" style={{ color: "var(--color-text-secondary)" }}>environment</td>
              <td>{data?.env ?? "—"}</td>
            </tr>
            <tr>
              <td className="py-1 pr-6" style={{ color: "var(--color-text-secondary)" }}>version</td>
              <td>{data?.version ?? "—"}</td>
            </tr>
            <tr>
              <td className="py-1 pr-6" style={{ color: "var(--color-text-secondary)" }}>database</td>
              <td>{data?.checks.database ?? "—"}</td>
            </tr>
          </tbody>
        </table>
      </Card>
      <p className="text-[13px]" style={{ color: "var(--color-text-secondary)" }}>
        Users &amp; roles, webhook secrets and cloud integrations are managed via their respective
        APIs; per-space RBAC settings arrive in a later phase.
      </p>
    </div>
  );
}
