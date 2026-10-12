import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { spaces as spacesApi, users, type UserUpdate } from "@/api/resources";
import type { Role } from "@/api/types";
import { useSpaces } from "@/app/SpaceContext";
import { useSession } from "@/auth/session";
import { Button } from "@/ui/Button";
import { ConfirmPopover } from "@/ui/ConfirmPopover";
import { Badge, Banner, Checkbox, NativeSelect, Skeleton } from "@/ui/primitives";
import { describeChange, diffUser, type Grant } from "./userChanges";

// Who may do what (DESIGN §5.7, §5.10): instance users and space members share one editor. Edits are
// staged and saved together behind a summary in words — a role or tier change is never one misclick.

const ROLES: Role[] = ["reader", "writer", "approver", "admin"];

function GrantCard({
  grant,
  draft,
  tierNames,
  onDraft,
  badges,
  canDisable,
  actions,
}: {
  grant: Grant;
  draft: UserUpdate;
  tierNames: string[];
  onDraft: (d: UserUpdate) => void;
  badges?: ReactNode;
  /** Instance users can be disabled (not oneself); space members cannot. */
  canDisable?: boolean;
  actions?: ReactNode;
}) {
  const role = draft.role ?? grant.role;
  const allowed = draft.allowed_tiers ?? grant.allowed_tiers;
  const canDestroy = draft.can_destroy ?? grant.can_destroy;
  const disabled = draft.disabled ?? grant.disabled ?? false;
  const changed = diffUser(grant, draft) !== null;
  return (
    <li
      className="flex flex-col gap-2 rounded-base border border-line bg-surface px-3 py-2 data-[changed=true]:border-control"
      data-changed={changed}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-data font-semibold">{grant.email}</span>
        {badges}
        {changed && <span className="font-data text-[12px] text-muted">· unsaved</span>}
        {actions && <span className="ml-auto">{actions}</span>}
      </div>
      <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-muted">Role</span>
          <NativeSelect value={role} onChange={(e) => onDraft({ ...draft, role: e.target.value as Role })}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </NativeSelect>
        </label>
        <fieldset className="flex flex-col gap-1">
          <legend className="eyebrow mb-1 text-muted">Allowed tiers</legend>
          <div className="flex h-8 flex-wrap items-center gap-3">
            {tierNames.map((t) => (
              <label key={t} className="flex items-center gap-1.5 font-data text-[12px]">
                <Checkbox
                  checked={allowed.includes(t)}
                  onChange={(e) =>
                    onDraft({
                      ...draft,
                      allowed_tiers: e.target.checked ? [...allowed, t] : allowed.filter((x) => x !== t),
                    })
                  }
                />
                {t}
              </label>
            ))}
          </div>
        </fieldset>
        <label className="flex h-8 items-center gap-1.5 text-ui">
          <Checkbox
            checked={canDestroy}
            onChange={(e) => onDraft({ ...draft, can_destroy: e.target.checked })}
          />
          May destroy
        </label>
        {canDisable !== undefined && (
          <label className="flex h-8 items-center gap-1.5 text-ui">
            <Checkbox
              checked={disabled}
              disabled={!canDisable}
              onChange={(e) => onDraft({ ...draft, disabled: e.target.checked })}
            />
            Disabled
          </label>
        )}
      </div>
    </li>
  );
}

interface Staged {
  drafts: Record<string, UserUpdate>;
  setDraft: (id: string, d: UserUpdate) => void;
  bar: ReactNode;
}

/** Drafts per row + the sticky "N changes to save" bar with the summary. */
function useStaged<T extends Grant & { id: string }>(
  rows: T[],
  label: string,
  saveOne: (row: T, change: UserUpdate) => Promise<unknown>,
  onSaved: () => void,
): Staged {
  const [drafts, setDrafts] = useState<Record<string, UserUpdate>>({});
  const changes = rows.flatMap((r) => {
    const c = drafts[r.id] ? diffUser(r, drafts[r.id]) : null;
    return c ? [{ row: r, change: c }] : [];
  });
  const lines = changes.flatMap(({ row, change }) => describeChange(row, change));
  const save = useMutation({
    mutationFn: async () => {
      for (const { row, change } of changes) {
        try {
          await saveOne(row, change);
        } catch (err) {
          throw new Error(`${row.email}: ${(err as Error).message}`, { cause: err });
        }
        setDrafts((d) => {
          const next = { ...d };
          delete next[row.id];
          return next;
        });
      }
    },
    onSettled: onSaved,
  });
  const bar =
    lines.length > 0 ? (
      <div
        role="region"
        aria-label={label}
        className="sticky bottom-0 flex max-w-[960px] flex-col gap-2 rounded-base border border-control bg-raised p-3 max-md:bottom-16"
      >
        <span className="eyebrow text-muted">
          {lines.length} change{lines.length > 1 ? "s" : ""} to save
        </span>
        <ul className="font-data text-[12px]">
          {lines.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
        {save.isError && (
          <Banner tone="failed" role="alert" title="The API refused">
            {(save.error as Error).message}
          </Banner>
        )}
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>
            Save {lines.length} change{lines.length > 1 ? "s" : ""}
          </Button>
          <Button variant="ghost" onClick={() => setDrafts({})}>
            Discard
          </Button>
        </div>
      </div>
    ) : null;
  return { drafts, setDraft: (id, d) => setDrafts((all) => ({ ...all, [id]: d })), bar };
}

export function Users({ tierNames }: { tierNames: string[] }) {
  const qc = useQueryClient();
  const { data: me } = useSession();
  const { data, isLoading } = useQuery({ queryKey: ["users"], queryFn: users.list });
  const list = [...(data ?? [])].sort((a, b) => a.email.localeCompare(b.email));
  const staged = useStaged(
    list,
    "Unsaved user changes",
    (u, change) => users.update(u.id, change),
    () => qc.invalidateQueries({ queryKey: ["users"] }),
  );
  return (
    <section aria-labelledby="users" className="flex flex-col gap-2">
      <h2 id="users" className="text-section font-semibold">
        Users
      </h2>
      <p className="max-w-[720px] text-[12px] text-muted">
        Instance-wide grants. Role gates what a user may do; allowed tiers are where they may confirm an
        apply. Changes are staged: review the summary, then save. Every change is audited.
      </p>
      {isLoading ? (
        <Skeleton className="h-40 w-full max-w-[960px]" />
      ) : (
        <ul className="flex max-w-[960px] flex-col gap-2">
          {list.map((u) => (
            <GrantCard
              key={u.id}
              grant={u}
              draft={staged.drafts[u.id] ?? {}}
              tierNames={tierNames}
              onDraft={(d) => staged.setDraft(u.id, d)}
              canDisable={u.id !== me?.id}
              badges={
                <>
                  {u.id === me?.id && <Badge>you</Badge>}
                  {u.disabled && <Badge glyph="⊘">disabled</Badge>}
                </>
              }
            />
          ))}
        </ul>
      )}
      {staged.bar}
    </section>
  );
}

/** Space members (DESIGN §5.10): per-space role and tiers, add and remove. */
export function SpaceMembers({ tierNames }: { tierNames: string[] }) {
  const qc = useQueryClient();
  const { list: spaces, current } = useSpaces();
  const [spaceId, setSpaceId] = useState<string | null>(null);
  const space = spaces.find((s) => s.id === (spaceId ?? current?.id)) ?? spaces[0];
  const key = ["space-members", space?.id];
  const members = useQuery({
    queryKey: key,
    queryFn: () => spacesApi.members(space!.id),
    enabled: !!space,
  });
  const userList = useQuery({ queryKey: ["users"], queryFn: users.list });
  const rows = (members.data ?? []).map((m) => ({ ...m, id: m.user_id }));
  const invalidate = () => qc.invalidateQueries({ queryKey: key });
  const staged = useStaged(
    rows,
    "Unsaved member changes",
    (m, change) =>
      spacesApi.setMember(space!.id, {
        user_id: m.user_id,
        role: change.role ?? m.role,
        allowed_tiers: change.allowed_tiers ?? m.allowed_tiers,
        can_destroy: change.can_destroy ?? m.can_destroy,
      }),
    invalidate,
  );
  const remove = useMutation({
    mutationFn: (userId: string) => spacesApi.removeMember(space!.id, userId),
    onSuccess: invalidate,
  });
  const [newUser, setNewUser] = useState("");
  const add = useMutation({
    mutationFn: () =>
      spacesApi.setMember(space!.id, {
        user_id: newUser,
        role: "reader",
        allowed_tiers: [],
        can_destroy: false,
      }),
    onSuccess: () => {
      setNewUser("");
      invalidate();
    },
  });
  const outsiders = (userList.data ?? []).filter((u) => !rows.some((m) => m.user_id === u.id));

  if (!space) return null;
  return (
    <section aria-labelledby="spaces" className="flex flex-col gap-2">
      <h2 id="spaces" className="text-section font-semibold">
        Spaces
      </h2>
      <p className="max-w-[720px] text-[12px] text-muted">
        A member&apos;s grants inside one space. New members start as readers with no tier: raise them here.
      </p>
      <label className="flex flex-col gap-1 self-start">
        <span className="eyebrow text-muted">Space</span>
        <NativeSelect value={space.id} onChange={(e) => setSpaceId(e.target.value)}>
          {spaces.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </NativeSelect>
      </label>
      {members.isLoading ? (
        <Skeleton className="h-32 w-full max-w-[960px]" />
      ) : rows.length === 0 ? (
        <p className="text-muted">No member in {space.name} yet.</p>
      ) : (
        <ul className="flex max-w-[960px] flex-col gap-2">
          {rows.map((m) => (
            <GrantCard
              key={m.user_id}
              grant={m}
              draft={staged.drafts[m.id] ?? {}}
              tierNames={tierNames}
              onDraft={(d) => staged.setDraft(m.id, d)}
              actions={
                <ConfirmPopover
                  trigger={
                    <Button size="sm" variant="ghost">
                      Remove…
                    </Button>
                  }
                  question={`${m.email} loses access to ${space.name}.`}
                  confirmLabel={`Remove ${m.email.split("@")[0]} from ${space.name}`}
                  onConfirm={() => remove.mutate(m.user_id)}
                />
              }
            />
          ))}
        </ul>
      )}
      {remove.isError && (
        <Banner tone="failed" role="alert" title="The API refused">
          {(remove.error as Error).message}
        </Banner>
      )}
      {staged.bar}
      {outsiders.length > 0 && (
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            add.mutate();
          }}
        >
          <label className="flex flex-col gap-1">
            <span className="eyebrow text-muted">Add a member</span>
            <NativeSelect value={newUser} onChange={(e) => setNewUser(e.target.value)}>
              <option value="">Pick a user…</option>
              {outsiders.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.email}
                </option>
              ))}
            </NativeSelect>
          </label>
          <Button type="submit" disabled={!newUser} loading={add.isPending}>
            Add to {space.name}
          </Button>
        </form>
      )}
      {add.isError && (
        <Banner tone="failed" role="alert" title="The API refused">
          {(add.error as Error).message}
        </Banner>
      )}
    </section>
  );
}
