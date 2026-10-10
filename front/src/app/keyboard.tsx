import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

// Keyboard shortcuts (DESIGN §4.3). Single-key shortcuts never fire while typing, and can be turned
// off from the user menu (WCAG 2.1.4); the setting lives in memory only (no browser storage).

interface KeyboardCtx {
  enabled: boolean;
  setEnabled: (v: boolean) => void;
}

const Ctx = createContext<KeyboardCtx>({ enabled: true, setEnabled: () => {} });

export function KeyboardProvider({ children }: { children: ReactNode }) {
  const [enabled, setEnabled] = useState(true);
  const value = useMemo(() => ({ enabled, setEnabled }), [enabled]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useKeyboardSettings(): KeyboardCtx {
  return useContext(Ctx);
}

function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

/**
 * Bind shortcuts: `{ "g o": fn, "?": fn, "mod+k": fn }`. Sequences are two keys within 1 s. `mod+`
 * combos (⌘ / Ctrl) work everywhere, even in fields; plain keys never fire while typing, inside an
 * open overlay, or when the user disabled single-key shortcuts.
 */
export function useHotkeys(bindings: Record<string, (e: KeyboardEvent) => void>, active = true): void {
  const { enabled } = useKeyboardSettings();
  const ref = useRef(bindings);
  useEffect(() => {
    ref.current = bindings;
  });
  useEffect(() => {
    if (!active) return;
    let pending: string | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onKey = (e: KeyboardEvent) => {
      const map = ref.current;
      if ((e.metaKey || e.ctrlKey) && !e.altKey) {
        const fn = map[`mod+${e.key.toLowerCase()}`];
        if (fn) {
          e.preventDefault();
          fn(e);
        }
        return;
      }
      if (!enabled || e.altKey || typing(e.target) || e.defaultPrevented) return;
      // An open overlay (palette, confirm popover, sheet) owns the keyboard. Radix marks its content
      // role="dialog" + data-state="open" (it does not set aria-modal).
      if (document.querySelector('[role="dialog"][data-state="open"]')) return;
      const key = e.key;
      if (pending) {
        const fn = map[`${pending} ${key}`];
        pending = null;
        if (timer) clearTimeout(timer);
        if (fn) {
          e.preventDefault();
          fn(e);
          return;
        }
      }
      if (Object.keys(map).some((k) => k.startsWith(`${key} `))) {
        pending = key;
        timer = setTimeout(() => (pending = null), 1000);
        return;
      }
      const fn = map[key];
      if (fn) {
        e.preventDefault();
        fn(e);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (timer) clearTimeout(timer);
    };
  }, [enabled, active]);
}
