import { useSyncExternalStore } from "react";

/** Subscribe to a CSS media query (e.g. the < 768 px on-call layout, DESIGN §4). */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const mq = window.matchMedia?.(query);
      mq?.addEventListener("change", onChange);
      return () => mq?.removeEventListener("change", onChange);
    },
    () => window.matchMedia?.(query).matches ?? false,
    () => false,
  );
}

export const MOBILE = "(max-width: 767px)";
