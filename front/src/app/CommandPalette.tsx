import { useMemo, useState, type ReactNode } from "react";
import { Command } from "cmdk";
import { useQueries, useQuery } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { stacks } from "@/api/resources";
import { useIsAdmin } from "@/auth/session";
import { useSpaces } from "./SpaceContext";
import { useTheme } from "./theme";
import { NAV } from "./nav";
import { Kbd } from "@/ui/primitives";

// ⌘K (DESIGN §4.2): the keyboard-first primary navigation. It never performs a state-changing
// action itself — actions on a run open the same confirmation as their button (stage 4 wires them).

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function CommandPalette({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const isAdmin = useIsAdmin();
  const { list: spaceList, spacePath, setCurrentId } = useSpaces();
  const { theme, toggle } = useTheme();
  const [query, setQuery] = useState("");

  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list, enabled: open });
  const envLists = useQueries({
    queries: (open ? (stackList.data ?? []) : []).map((s) => ({
      queryKey: ["environments", s.id],
      queryFn: () => stacks.environments(s.id),
    })),
  });

  const spaceName = useMemo(() => new Map(spaceList.map((s) => [s.id, s.name])), [spaceList]);
  const stackPath = (spaceId: string, name: string) =>
    `/s/${encodeURIComponent(spaceName.get(spaceId) ?? "")}/stacks/${encodeURIComponent(name)}`;

  const go = (to: string) => {
    onOpenChange(false);
    setQuery("");
    navigate(to);
  };
  const run = (fn: () => void) => {
    onOpenChange(false);
    setQuery("");
    fn();
  };

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-scrim" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className="fixed top-[12vh] left-1/2 z-50 w-[calc(100vw-32px)] max-w-[640px] -translate-x-1/2 overflow-hidden rounded-base border border-control bg-raised text-primary"
        >
          <DialogPrimitive.Title className="sr-only">Command palette</DialogPrimitive.Title>
          <Command label="Command palette" loop>
            <div className="flex items-center gap-2 border-b border-line px-3">
              <Command.Input
                value={query}
                onValueChange={setQuery}
                placeholder="Go to a stack, an environment, a page, or paste a run ID…"
                className="h-11 flex-1 bg-transparent font-data text-ui text-primary outline-none placeholder:text-muted"
              />
              <Kbd>esc</Kbd>
            </div>
            <Command.List className="max-h-[60vh] overflow-auto p-1">
              <Command.Empty className="px-3 py-6 text-muted">No match.</Command.Empty>

              {UUID.test(query.trim()) && (
                <Group heading="Run">
                  <Item value={`run ${query}`} onSelect={() => go(`/runs/${query.trim()}`)}>
                    Open run <span className="font-data">{query.trim().slice(0, 8)}</span>
                  </Item>
                </Group>
              )}

              <Group heading="Go to">
                {NAV.filter((n) => !n.adminOnly || isAdmin).map((n) => (
                  <Item key={n.label} value={`page ${n.label}`} onSelect={() => go(n.to(spacePath))}>
                    <n.icon size={15} strokeWidth={1.5} aria-hidden className="text-muted" />
                    {n.label}
                  </Item>
                ))}
              </Group>

              {(stackList.data?.length ?? 0) > 0 && (
                <Group heading="Stacks">
                  {stackList.data!.map((s) => (
                    <Item
                      key={s.id}
                      value={`stack ${s.name} ${spaceName.get(s.space_id)}`}
                      onSelect={() => go(stackPath(s.space_id, s.name))}
                    >
                      <span className="font-data">{s.name}</span>
                      <span className="ml-auto font-data text-[12px] text-muted">
                        {spaceName.get(s.space_id)}
                      </span>
                    </Item>
                  ))}
                </Group>
              )}

              {envLists.some((q) => q.data?.length) && (
                <Group heading="Environments">
                  {(stackList.data ?? []).flatMap((s, i) =>
                    (envLists[i]?.data ?? []).map((e) => (
                      <Item
                        key={e.id}
                        value={`env ${s.name}/${e.name} ${e.tier}`}
                        onSelect={() =>
                          go(`${stackPath(s.space_id, s.name)}/envs/${encodeURIComponent(e.name)}`)
                        }
                      >
                        <span className="font-data">
                          {s.name}/{e.name}
                        </span>
                        <span className="ml-auto font-data text-[12px] text-muted">tier {e.tier}</span>
                      </Item>
                    )),
                  )}
                </Group>
              )}

              <Group heading="Actions">
                <Item value="action toggle theme" onSelect={() => run(toggle)}>
                  Switch to {theme === "dark" ? "light" : "dark"} theme
                </Item>
                <Item
                  value="action copy link url"
                  onSelect={() =>
                    run(
                      () =>
                        void navigator.clipboard?.writeText(
                          window.location.origin + location.pathname + location.search,
                        ),
                    )
                  }
                >
                  Copy the link to this page
                </Item>
              </Group>

              {spaceList.length > 1 && (
                <Group heading="Spaces">
                  {spaceList.map((s) => (
                    <Item key={s.id} value={`space ${s.name}`} onSelect={() => run(() => setCurrentId(s.id))}>
                      Switch to space <span className="font-data">{s.name}</span>
                    </Item>
                  ))}
                </Group>
              )}
            </Command.List>
          </Command>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

function Group({ heading, children }: { heading: string; children: ReactNode }) {
  return (
    <Command.Group
      heading={heading}
      className="mb-1 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:font-data [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:tracking-[0.04em] [&_[cmdk-group-heading]]:text-muted [&_[cmdk-group-heading]]:uppercase"
    >
      {children}
    </Command.Group>
  );
}

function Item({ value, onSelect, children }: { value: string; onSelect: () => void; children: ReactNode }) {
  return (
    <Command.Item
      value={value}
      onSelect={onSelect}
      className="flex cursor-pointer items-center gap-2 rounded-base px-2 py-1.5 data-[selected=true]:bg-surface data-[selected=true]:outline data-[selected=true]:outline-1 data-[selected=true]:outline-ring"
    >
      {children}
    </Command.Item>
  );
}
