import type { GlobalProvider } from "@ladle/react";
import "../src/index.css";

// Stories render on the dark blueprint surface with the real design tokens (DESIGN §8).
export const Provider: GlobalProvider = ({ children }) => (
  <div
    data-theme="dark"
    style={{ minHeight: "100vh", padding: 32, backgroundColor: "var(--color-bg-base)", color: "var(--color-text-primary)" }}
  >
    {children}
  </div>
);
