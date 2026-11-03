import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

// One polite live region for the whole app (DESIGN §7): real-time changes are announced, never by
// moving focus.

const Ctx = createContext<(message: string) => void>(() => {});

export function AnnouncerProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState("");
  const announce = useCallback((m: string) => {
    // Clear first so repeating the same message is announced again.
    setMessage("");
    setTimeout(() => setMessage(m), 50);
  }, []);
  return (
    <Ctx.Provider value={announce}>
      {children}
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {message}
      </div>
    </Ctx.Provider>
  );
}

export function useAnnounce(): (message: string) => void {
  return useContext(Ctx);
}
