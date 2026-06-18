import { defineConfig, devices } from "@playwright/test";

// One happy-path smoke against a running dev stack (CI job, Phase G). The runner must have the API
// (task dev) up and the Vite dev server serving the SPA — point PW_BASE_URL at it.
const baseURL = process.env.PW_BASE_URL ?? "http://localhost:5173";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  use: { baseURL, trace: "on-first-retry" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
