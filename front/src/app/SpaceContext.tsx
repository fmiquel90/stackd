import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { spaces } from "@/api/resources";
import type { Space } from "@/api/types";

interface SpaceCtx {
  list: Space[];
  /** The active space, or null while loading / when the user belongs to none. */
  current: Space | null;
  setCurrentId: (id: string) => void;
}

const Ctx = createContext<SpaceCtx | null>(null);

// Selection is in-memory only (no browser storage — invariant #8). The active space scopes the
// stacks view and is the default target when creating a stack (§6, Phase F).
export function SpaceProvider({ children }: { children: ReactNode }) {
  const { data } = useQuery({ queryKey: ["spaces"], queryFn: spaces.list });
  const [currentId, setCurrentId] = useState<string | null>(null);
  const list = useMemo(() => data ?? [], [data]);
  const current = useMemo(() => {
    if (!list.length) return null;
    return list.find((s) => s.id === currentId) ?? list[0];
  }, [list, currentId]);
  const value = useMemo<SpaceCtx>(() => ({ list, current, setCurrentId }), [list, current]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSpaces(): SpaceCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useSpaces must be used within a SpaceProvider");
  return ctx;
}
