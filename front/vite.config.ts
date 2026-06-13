import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

const apiTarget = process.env.VITE_API_PROXY ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  server: {
    host: true,
    port: 5173,
    // Same-origin proxy → no CORS, session cookies stay first-party (DESIGN §8).
    proxy: {
      // ws: true also proxies the /api/v1/ws WebSocket upgrade to the API.
      "/api": { target: apiTarget, changeOrigin: true, ws: true },
    },
  },
});
