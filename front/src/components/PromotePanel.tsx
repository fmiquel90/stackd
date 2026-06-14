import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { runs } from "@/api/resources";
import { Button, Card, Field, Select } from "@/components/ui";

// Promote the commit currently applied on a sibling environment of the same stack to this one.
export function PromotePanel({
  envId,
  siblings,
}: {
  envId: string;
  siblings: { id: string; name: string }[];
}) {
  const navigate = useNavigate();
  const [from, setFrom] = useState(siblings[0]?.id ?? "");

  const promote = useMutation({
    mutationFn: () => runs.promote(envId, from),
    onSuccess: (r) => navigate(`/runs/${r.id}`),
  });

  if (siblings.length === 0) {
    return (
      <Card>
        <div className="text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          No sibling environment to promote from — add another environment to this stack.
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <div className="mb-1 text-[13px] font-medium">Promote to this environment</div>
      <div className="mb-2 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        Re-deploy the exact commit currently applied on another environment of this stack here. The
        apply is gated as usual (tier + four-eyes) at confirmation.
      </div>
      <form
        className="flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          promote.mutate();
        }}
      >
        <Field label="From environment">
          <Select value={from} onChange={(e) => setFrom(e.target.value)}>
            {siblings.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </Select>
        </Field>
        <Button type="submit" disabled={promote.isPending || !from}>
          Promote here →
        </Button>
      </form>
      {promote.isError && (
        <div className="mt-2 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {(promote.error as Error).message}
        </div>
      )}
    </Card>
  );
}
