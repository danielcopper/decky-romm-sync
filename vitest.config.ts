import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "happy-dom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.{test,spec}.{ts,tsx}",
        "src/test-setup.ts",
        // Aligned with sonar-project.properties `sonar.coverage.exclusions`.
        "src/types/**",
        "src/index.tsx",
        "src/patches/**",
        "src/utils/styleInjector.ts",
      ],
    },
  },
});
