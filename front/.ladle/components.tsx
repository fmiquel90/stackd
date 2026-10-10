import type { GlobalProvider } from "@ladle/react";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "../src/ui/overlays";
import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-500.css";
import "../src/index.css";

// Every story renders in BOTH themes side by side, on the real tokens (DESIGN §8: the story is the
// component's contract, and the light theme is a full peer).
export const Provider: GlobalProvider = ({ children }) => (
  <MemoryRouter>
    <TooltipProvider>
      <div className="grid min-h-screen grid-cols-1 xl:grid-cols-2">
        {(["dark", "light"] as const).map((theme) => (
          <section key={theme} data-theme={theme} className="min-w-0 bg-base p-6 text-primary">
            <div className="eyebrow mb-4 text-muted">{theme} theme</div>
            {children}
          </section>
        ))}
      </div>
    </TooltipProvider>
  </MemoryRouter>
);
