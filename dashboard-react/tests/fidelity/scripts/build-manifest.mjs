// Regenerate manifest.json from the committed corpus. Reproducible from the corpus
// alone (no coupling to live repo docs): it hashes each corpus copy and detects
// which optional schema-node extensions (table, taskList, image) the file needs.
// Run from the fidelity package: `node scripts/build-manifest.mjs`.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { MarkdownManager } from "@tiptap/markdown";
import StarterKit from "@tiptap/starter-kit";
import { Table, TableRow, TableHeader, TableCell } from "@tiptap/extension-table";
import { TaskList, TaskItem } from "@tiptap/extension-list";
import Image from "@tiptap/extension-image";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const FIDELITY_DIR = path.resolve(SCRIPT_DIR, "..");
const REPO_ROOT = path.resolve(FIDELITY_DIR, "../../..");
const CORPUS_DIR = path.join(FIDELITY_DIR, "corpus");
const MANIFEST_PATH = path.join(FIDELITY_DIR, "manifest.json");

const mm = new MarkdownManager({
  extensions: [StarterKit, Table, TableRow, TableHeader, TableCell, TaskList, TaskItem, Image],
});

const OPTIONAL_NODE_TO_EXT = {
  table: "table",
  tableRow: "table",
  tableHeader: "table",
  tableCell: "table",
  taskList: "taskList",
  taskItem: "taskList",
  image: "image",
};

const lf = (s) => s.replace(/\r\n/g, "\n");
const sha256 = (s) => createHash("sha256").update(s, "utf8").digest("hex");
const splitFm = (src) => {
  const m = src.match(/^(---\n[\s\S]*?\n---\n?)([\s\S]*)$/);
  return m ? { fm: m[1], body: m[2] } : { fm: "", body: src };
};

function collectNodeTypes(node, into) {
  if (node && typeof node.type === "string") into.add(node.type);
  if (node && Array.isArray(node.content)) {
    for (const child of node.content) collectNodeTypes(child, into);
  }
  return into;
}

function requiredExtensions(body) {
  const types = collectNodeTypes(mm.parse(body), new Set());
  const exts = new Set();
  for (const type of types) {
    if (OPTIONAL_NODE_TO_EXT[type]) exts.add(OPTIONAL_NODE_TO_EXT[type]);
  }
  return [...exts].sort();
}

function listCorpus() {
  const out = [];
  for (const sub of ["real", "synthetic"]) {
    const dir = path.join(CORPUS_DIR, sub);
    for (const file of fs.readdirSync(dir).sort()) {
      if (file.endsWith(".md")) out.push(path.join(dir, file));
    }
  }
  return out;
}

const entries = [];
for (const abs of listCorpus()) {
  const source = lf(fs.readFileSync(abs, "utf8"));
  const rel = path.relative(REPO_ROOT, abs).split(path.sep).join("/");
  const { body } = splitFm(source);
  entries.push({
    path: rel,
    expected_sha256: sha256(source),
    required_extensions: requiredExtensions(body),
  });
}

const manifest = { schema_version: "wb-fidelity-corpus/v1", entries };
fs.writeFileSync(MANIFEST_PATH, JSON.stringify(manifest, null, 2) + "\n", "utf8");
console.log(`wrote ${entries.length} entries to ${path.relative(REPO_ROOT, MANIFEST_PATH)}`);
