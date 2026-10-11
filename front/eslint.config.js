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

// Colour lives in tokens only (CLAUDE §4, DESIGN §8): an inline colour style is an error.
const NO_INLINE_COLOUR = {
  selector: `JSXAttribute[name.name='style'] Property[key.name=${COLOR_PROPS}]`,
  message: "No inline colour style: use the token utilities (DESIGN §8).",
};

// The decision documents (SPECS, DESIGN, AUDIT…) are for the team, never for users: no "§x.y",
// "SPECS.md" or "AUDIT A5" in text the UI can show. Comments are not AST nodes, so they stay free.
const DOC_REF = "/§|\\b(SPECS|DESIGN|AUDIT|PLAN|DEV|CLAUDE)(\\.md|\\s+A[0-9])/";
const NO_DOC_REF = [
  `Literal[value=${DOC_REF}]`,
  `TemplateElement[value.raw=${DOC_REF}]`,
  `JSXText[value=${DOC_REF}]`,
].map((selector) => ({
  selector,
  message: "No reference to the project's decision documents in user-facing text.",
}));

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
      // Our form primitives render a real <input>/<select>: a <label> wrapping them is associated.
      "jsx-a11y/label-has-associated-control": [
        "error",
        { controlComponents: ["Input", "NativeSelect", "Checkbox"], depth: 3 },
      ],
      "no-restricted-syntax": ["error", NO_INLINE_COLOUR, ...NO_DOC_REF],
    },
  },
  {
    // Tests and stories are read by the team: their names may cite the documents.
    files: ["**/*.test.{ts,tsx}", "**/*.stories.tsx", "**/__fixtures__/**", ".ladle/**", "e2e/**"],
    rules: { "no-restricted-syntax": ["error", NO_INLINE_COLOUR] },
  },
  {
    files: ["*.config.{js,ts}", "e2e/**/*.ts", "scripts/**/*.{js,mjs}"],
    languageOptions: { globals: { ...globals.node } },
  },
);
