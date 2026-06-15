import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { observability, tiers, type UserUpdate, users } from "@/api/resources";
import type { Role, TierDef, User } from "@/api/types";
import { useIsAdmin } from "@/auth/session";
import { Button, Card, Field, PageTitle, Select, TextInput } from "@/components/ui";

const ROLES: Role[] = ["reader", "writer", "approver", "admin"];

function UserRow({ user, tierNames }: { user: User; tierNames: string[] }) {
  const qc = useQueryClient();
  const update = useMutation({
    mutationFn: (body: UserUpdate) => users.update(user.id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });
  const cell = "py-1 pr-4 align-top";
  const toggleTier = (name: string) => {
    const has = user.allowed_tiers.includes(name);
    const next = has ? user.allowed_tiers.filter((t) => t !== name) : [...user.allowed_tiers, name];
    update.mutate({ allowed_tiers: next });
  };
  return (
    <tr>
      <td className={cell}>{user.email}</td>
      <td className={cell}>
        <Select
          value={user.role}
          disabled={update.isPending}
          onChange={(e) => update.mutate({ role: e.target.value as Role })}
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </Select>
      </td>
      <td className={cell}>
        {/* Set membership — any combination of tiers, incl. non-contiguous (§2.4). */}
        <span className="flex flex-wrap gap-2">
          {tierNames.map((name) => (
            <label key={name} className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={user.allowed_tiers.includes(name)}
                disabled={update.isPending}
                onChange={() => toggleTier(name)}
              />
              {name}
            </label>
          ))}
        </span>
      </td>
      <td className={cell}>
        <label className="flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={user.can_destroy}
            disabled={update.isPending}
            onChange={(e) => update.mutate({ can_destroy: e.target.checked })}
          />
          can destroy
        </label>
      </td>
      <td className={cell}>
        <label className="flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={user.disabled}
            disabled={update.isPending}
            onChange={(e) => update.mutate({ disabled: e.target.checked })}
          />
          disabled
        </label>
      </td>
    </tr>
  );
}

function UsersPanel({ tierNames }: { tierNames: string[] }) {
  const { data } = useQuery({ queryKey: ["users"], queryFn: users.list });
  return (
    <Card>
      <div className="mb-1 text-[13px] font-medium">Users &amp; roles</div>
      <div className="mb-3 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        Role gates what a user may do; <span className="font-data">allowed tiers</span> is the set of
        tiers they can confirm an apply on — any combination, including all or none. Every change is
        audited.
      </div>
      <table className="w-full text-left font-data text-[12px]">
        <thead>
          <tr style={{ color: "var(--color-text-secondary)" }}>
            <th className="py-1 pr-4">EMAIL</th>
            <th className="py-1 pr-4">ROLE</th>
            <th className="py-1 pr-4">ALLOWED TIERS</th>
            <th className="py-1 pr-4">DESTROY</th>
            <th className="py-1 pr-4">STATUS</th>
          </tr>
        </thead>
        <tbody>
          {(data ?? []).map((u) => (
            <UserRow key={u.id} user={u} tierNames={tierNames} />
          ))}
        </tbody>
      </table>
      {!data && <span className="font-data text-[12px]">Loading…</span>}
    </Card>
  );
}

function TiersPanel({ catalog }: { catalog: TierDef[] }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [fourEyes, setFourEyes] = useState(false);
  const invalidate = () => qc.invalidateQueries({ queryKey: ["tiers"] });

  const create = useMutation({
    mutationFn: () => tiers.create({ name, requires_four_eyes: fourEyes, position: catalog.length }),
    onSuccess: () => {
      invalidate();
      setName("");
      setFourEyes(false);
    },
  });
  const toggle = useMutation({
    mutationFn: (t: TierDef) => tiers.update(t.id, { requires_four_eyes: !t.requires_four_eyes }),
    onSuccess: invalidate,
  });
  const remove = useMutation({ mutationFn: (id: string) => tiers.remove(id), onSuccess: invalidate });

  return (
    <Card>
      <div className="mb-1 text-[13px] font-medium">Tiers</div>
      <div className="mb-3 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        Tiers classify environments and are routed to workers by label. A tier with{" "}
        <span className="font-data">four-eyes</span> forces a second person to confirm applies.
      </div>
      <div className="flex flex-col gap-1">
        {catalog.map((t) => (
          <div key={t.id} className="font-data flex items-center gap-3 text-[12px]">
            <span style={{ minWidth: 100 }}>{t.name}</span>
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={t.requires_four_eyes}
                disabled={toggle.isPending}
                onChange={() => toggle.mutate(t)}
              />
              four-eyes
            </label>
            <button
              type="button"
              aria-label={`Delete ${t.name}`}
              className="ui-btn"
              style={{ color: "var(--color-state-failed)" }}
              onClick={() => remove.mutate(t.id)}
            >
              <X size={13} strokeWidth={1.75} aria-hidden />
            </button>
          </div>
        ))}
      </div>
      <form
        className="mt-3 flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="New tier">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="qa" required />
        </Field>
        <label className="flex items-center gap-1.5 pb-1.5 text-[13px]">
          <input type="checkbox" checked={fourEyes} onChange={(e) => setFourEyes(e.target.checked)} />
          four-eyes
        </label>
        <Button type="submit" disabled={create.isPending}>
          Add tier
        </Button>
      </form>
      {(create.isError || remove.isError) && (
        <div className="mt-2 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {((create.error ?? remove.error) as Error).message}
        </div>
      )}
    </Card>
  );
}

export function SettingsPage() {
  const isAdmin = useIsAdmin();
  const { data } = useQuery({ queryKey: ["health"], queryFn: observability.health });
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list, enabled: isAdmin });
  const tierNames = (catalog.data ?? []).map((t) => t.name);

  return (
    <div className="flex flex-col gap-4">
      <PageTitle>Settings</PageTitle>

      {isAdmin && <TiersPanel catalog={catalog.data ?? []} />}
      {isAdmin && <UsersPanel tierNames={tierNames} />}

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

      {!isAdmin && (
        <p className="text-[13px]" style={{ color: "var(--color-text-secondary)" }}>
          User, role &amp; tier administration is restricted to admins.
        </p>
      )}
    </div>
  );
}
