// Flat config (ESLint 9). Lint is part of the front's definition of done (CLAUDE §5, DESIGN §8).
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import globals from "globals";

// Inline colour styles bypass the token utilities (DESIGN §8). Geometry (virtualized heights,
// graph positions) may still use `style`; colour properties may not.
const COLOR_PROPS =
  "/^(color|background|backgroundColor|border|borderColor|borderTop|borderRight|borderBottom|borderLeft|borderTopColor|borderBottomColor|borderLeftColor|borderRightColor|fill|stroke|outline|outlineColor|boxShadow)$/";

export default tseslint.config(
  { ignores: ["dist", "build", "node_modules", "playwright-report", "test-results"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,
  jsxA11y.flatConfigs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { globals: { ...globals.browser }, ecmaVersion: 2023 },
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      // No browser storage (CLAUDE invariant #8).
      "no-restricted-globals": [
        "error",
        { name: "localStorage", message: "No browser storage (CLAUDE §4.8)." },
        { name: "sessionStorage", message: "No browser storage (CLAUDE §4.8)." },
      ],
      "no-restricted-properties": [
        "error",
        { object: "window", property: "localStorage", message: "No browser storage (CLAUDE §4.8)." },
        { object: "window", property: "sessionStorage", message: "No browser storage (CLAUDE §4.8)." },
      ],
      // Focus moved by autoFocus is fine when the field appears because the user just asked for it
      // (open a composer, open the confirm popover) — DESIGN §7 forbids only focus moved by events.
      "jsx-a11y/no-autofocus": "off",
      // "warn" while the pre-redesign pages still carry inline colours; switched to "error" once
      // every screen is migrated (redesign stage 4).
      "no-restricted-syntax": [
        "warn",
        {
          selector: `JSXAttribute[name.name='style'] Property[key.name=${COLOR_PROPS}]`,
          message: "No inline colour style: use the token utilities (DESIGN §8).",
        },
      ],
    },
  },
  {
    files: ["*.config.{js,ts}", "e2e/**/*.ts", "scripts/**/*.{js,mjs}"],
    languageOptions: { globals: { ...globals.node } },
  },
);
