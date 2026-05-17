import js from "@eslint/js";
import tseslint from "typescript-eslint";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import globals from "globals";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "defaults", "bin", "coverage", ".worktrees", "py_modules/vdf"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  react.configs.flat.recommended,
  react.configs.flat["jsx-runtime"],
  reactHooks.configs.flat["recommended-latest"],
  jsxA11y.flatConfigs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      globals: {
        ...globals.browser,
        SteamClient: "readonly",
        appStore: "readonly",
        appDetailsStore: "readonly",
        appDetailsCache: "readonly",
        collectionStore: "readonly",
      },
    },
    settings: { react: { version: "detect" } },
    rules: {
      "react/prop-types": "off", // TS handles this
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
      // Downgraded to warn — 56 occurrences across the codebase, mostly in
      // Steam-UI patching code where the upstream API is genuinely untyped.
      // Cleanup tracked in #617. Promote back to error when zero.
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
  {
    // Ambient global type declarations require `var` and `any` by their nature.
    files: ["**/*.d.ts"],
    rules: {
      "no-var": "off",
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
);
