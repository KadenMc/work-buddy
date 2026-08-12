import { readdir, stat } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const output = resolve(
  fileURLToPath(new URL(".", import.meta.url)),
  "../../work_buddy/document_kernel/runtime_dist",
);
const entries = (await readdir(output)).sort();
if (entries.length !== 1 || entries[0] !== "worker.mjs") {
  throw new Error(
    `Document-kernel build must contain only worker.mjs; found ${entries.join(", ")}`,
  );
}
const worker = await stat(resolve(output, "worker.mjs"));
if (!worker.isFile() || worker.size === 0) {
  throw new Error("Document-kernel worker.mjs is missing or empty");
}
