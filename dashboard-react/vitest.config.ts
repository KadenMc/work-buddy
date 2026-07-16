import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    passWithNoTests: true,
    // IndexedDB-style draft restoration and lazy widget modules introduce real async
    // boundaries. Keep the gate deterministic when the full suite runs in parallel.
    testTimeout: 15_000,
  },
});
