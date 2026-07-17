import { defineConfig } from "vitest/config";

// The fidelity gate is a DOM-free serializer suite (SP-3 confirmed the standalone
// MarkdownManager runs without a DOM), so it uses the node environment and its own
// config, isolated from the dashboard root vitest config which is scoped to src/.
export default defineConfig({
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
    // Materialization timing at 10k words plus 30-plus corpus files parsing through
    // a beta serializer. Keep the gate deterministic under a generous ceiling.
    testTimeout: 30_000,
  },
});
