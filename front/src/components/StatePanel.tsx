import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { stateApi } from "@/api/resources";
import { Button, Card } from "@/components/ui";

export function StatePanel({ envId }: { envId: string }) {
  const qc = useQueryClient();
  const key = ["state-versions", envId];
  const { data } = useQuery({ queryKey: key, queryFn: () => stateApi.versions(envId) });
  const unlock = useMutation({
    mutationFn: () => stateApi.forceUnlock(envId),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });

  return (
    <Card>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[13px] font-medium">Managed state</span>
        <Button onClick={() => unlock.mutate()} disabled={unlock.isPending}>
          Force unlock
        </Button>
      </div>
      {data && data.length > 0 ? (
        <table className="w-full text-left font-data text-[12px]">
          <thead>
            <tr style={{ color: "var(--color-text-secondary)" }}>
              <th className="py-1 pr-4">SERIAL</th>
              <th className="py-1 pr-4">SIZE</th>
              <th className="py-1 pr-4">RUN</th>
              <th className="py-1">CREATED</th>
            </tr>
          </thead>
          <tbody>
            {data.map((v) => (
              <tr key={v.id}>
                <td className="py-1 pr-4">{v.serial}</td>
                <td className="py-1 pr-4">{v.size_bytes} B</td>
                <td className="py-1 pr-4">{v.created_by_run_id ? v.created_by_run_id.slice(0, 8) : "—"}</td>
                <td className="py-1" style={{ color: "var(--color-text-secondary)" }}>
                  {new Date(v.created_at).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          No state versions yet (the first apply writes one).
        </span>
      )}
    </Card>
  );
}
