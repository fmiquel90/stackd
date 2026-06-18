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
  build: {
    rollupOptions: {
      output: {
        // Isolate the heavy graph stack (@xyflow/react + dagre + d3) so it ships only with /graph,
        // and split the React runtime into a long-cached vendor chunk (DESIGN §8, Phase G).
        manualChunks(id) {
          if (id.includes("@xyflow") || id.includes("dagre") || id.includes("d3-")) return "graph";
          if (id.includes("react-dom") || id.includes("react-router")) return "react-vendor";
        },
      },
    },
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
