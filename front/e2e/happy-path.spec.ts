import { expect, test } from "@playwright/test";

// Dev-login happy path (Phase G). Against `task dev`, signing in as the admin persona lands on the
// Stacks view with the primary nav present. The deeper plan→confirm flow is covered by the API e2e
// (api/e2e/test_scenario.py); this guards the SPA shell + auth wiring end to end.
test("dev-login reaches the Stacks view", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /admin@dev\.local/ }).click();
  await expect(page).toHaveURL(/\/stacks/);
  await expect(page.getByRole("link", { name: "Stacks" })).toBeVisible();
});
