import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type Theme = "dark" | "light";

interface ThemeCtx {
  theme: Theme;
  /** True when the user overrode the OS preference in this tab (kept in memory only). */
  overridden: boolean;
  toggle: () => void;
  /** Follow the OS (`null`) or force a theme, for this tab. */
  setOverride: (t: Theme | null) => void;
}

const Ctx = createContext<ThemeCtx | null>(null);
const QUERY = "(prefers-color-scheme: light)";

function systemTheme(): Theme {
  return typeof window !== "undefined" && window.matchMedia?.(QUERY).matches ? "light" : "dark";
}

/**
 * Theme = the OS preference, unless the user toggles it; the override lives in memory and is lost on
 * reload (no browser storage — CLAUDE §4.8; server-side persistence needs AUDIT A8). index.html sets
 * the attribute before first paint so there is no flash.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [system, setSystem] = useState<Theme>(systemTheme);
  const [override, setOverride] = useState<Theme | null>(null);
  const theme = override ?? system;

  useEffect(() => {
    const mq = window.matchMedia?.(QUERY);
    if (!mq) return;
    const onChange = () => setSystem(mq.matches ? "light" : "dark");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const toggle = useCallback(() => setOverride(theme === "dark" ? "light" : "dark"), [theme]);
  const value = useMemo(
    () => ({ theme, overridden: override !== null, toggle, setOverride }),
    [theme, override, toggle],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useTheme must be used within a ThemeProvider");
  return ctx;
}
