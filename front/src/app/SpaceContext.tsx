import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useMatch, useNavigate } from "react-router-dom";
import { spaces, stacks } from "@/api/resources";
import type { Space } from "@/api/types";

interface SpaceCtx {
  list: Space[];
  /** The active space: the URL's (`/s/:space`), else the viewed entity's, else the default. */
  current: Space | null;
  /** First space (by name) that has stacks, else the first space — never an empty landing (audit C6). */
  fallback: Space | null;
  /** Switch space: navigates to its overview (the space lives in the URL, DESIGN §4.1). */
  setCurrentId: (id: string) => void;
  /** `/s/<current>` + path. */
  spacePath: (path?: string) => string;
  /** Pages outside `/s/:space` (a run) declare the space of what they show. */
  setEntitySpace: (id: string | null) => void;
  loading: boolean;
}

const Ctx = createContext<SpaceCtx | null>(null);

export function SpaceProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const spaceList = useQuery({ queryKey: ["spaces"], queryFn: spaces.list });
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  const deep = useMatch("/s/:space/*");
  const root = useMatch("/s/:space");
  const urlName = (deep ?? root)?.params.space;
  const [entityId, setEntitySpace] = useState<string | null>(null);

  const list = useMemo(
    () => [...(spaceList.data ?? [])].sort((a, b) => a.name.localeCompare(b.name)),
    [spaceList.data],
  );
  const fallback = useMemo(() => {
    const used = new Set((stackList.data ?? []).map((s) => s.space_id));
    return list.find((s) => used.has(s.id)) ?? list[0] ?? null;
  }, [list, stackList.data]);

  const current =
    (urlName ? list.find((s) => s.name === urlName) : undefined) ??
    (entityId ? list.find((s) => s.id === entityId) : undefined) ??
    fallback;

  const setCurrentId = useCallback(
    (id: string) => {
      const s = list.find((x) => x.id === id);
      if (s) navigate(`/s/${encodeURIComponent(s.name)}`);
    },
    [list, navigate],
  );
  const spacePath = useCallback(
    (path = "") => (current ? `/s/${encodeURIComponent(current.name)}${path}` : "/"),
    [current],
  );

  const value = useMemo<SpaceCtx>(
    () => ({
      list,
      current,
      fallback,
      setCurrentId,
      spacePath,
      setEntitySpace,
      loading: spaceList.isLoading || stackList.isLoading,
    }),
    [list, current, fallback, setCurrentId, spacePath, spaceList.isLoading, stackList.isLoading],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSpaces(): SpaceCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useSpaces must be used within a SpaceProvider");
  return ctx;
}

/** Declare the space of the entity a page shows (run page: run → env → stack → space). */
export function useEntitySpace(spaceId: string | null | undefined): void {
  const { setEntitySpace } = useSpaces();
  useEffect(() => {
    if (!spaceId) return;
    setEntitySpace(spaceId);
    return () => setEntitySpace(null);
  }, [spaceId, setEntitySpace]);
}
