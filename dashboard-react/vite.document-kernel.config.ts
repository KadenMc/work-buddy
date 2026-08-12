import { defineConfig } from "vite";
import { resolve } from "node:path";

export default defineConfig({
  publicDir: false,
  build: {
    target: "node20",
    outDir: resolve(__dirname, "../work_buddy/document_kernel/runtime_dist"),
    emptyOutDir: true,
    minify: false,
    lib: {
      entry: resolve(
        __dirname,
        "src/apps/cowork/document-kernel/worker.ts",
      ),
      formats: ["es"],
      fileName: () => "worker.mjs",
    },
    rollupOptions: {
      external: [/^node:/u],
    },
  },
});
