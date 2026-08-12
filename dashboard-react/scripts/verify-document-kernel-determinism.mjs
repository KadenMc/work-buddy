import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL(".", import.meta.url)), "..");
const vite = resolve(root, "node_modules/vite/bin/vite.js");
const verifier = resolve(root, "scripts/verify-document-kernel-build.mjs");
const worker = resolve(
  root,
  "../work_buddy/document_kernel/runtime_dist/worker.mjs",
);

const build = async () => {
  execFileSync(
    process.execPath,
    [vite, "build", "--config", "vite.document-kernel.config.ts"],
    { cwd: root, stdio: "inherit" },
  );
  execFileSync(process.execPath, [verifier], { cwd: root, stdio: "inherit" });
  return createHash("sha256").update(await readFile(worker)).digest("hex");
};

const first = await build();
const second = await build();
if (first !== second) {
  throw new Error(`Document-kernel build is not deterministic: ${first} != ${second}`);
}
