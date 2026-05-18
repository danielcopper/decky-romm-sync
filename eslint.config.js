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
      // Promoted back to error in #617 cleanup. Untyped sites that genuinely
      // need `any` (Steam internal React tree walking in src/patches/) carry
      // an inline `// eslint-disable-next-line @typescript-eslint/no-explicit-any`
      // with a documented reason.
      "@typescript-eslint/no-explicit-any": "error",
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
  {
    // Vitest globals (describe/it/expect/vi/...) are injected at runtime via
    // vitest.config.ts `globals: true` + tsconfig "types": ["vitest/globals"].
    files: ["src/**/*.{test,spec}.{ts,tsx}", "src/test-setup.ts"],
    languageOptions: {
      globals: { ...globals.vitest },
    },
    rules: {
      // Anonymous mock components are fine — they don't appear in real render trees.
      "react/display-name": "off",
    },
  },
);
