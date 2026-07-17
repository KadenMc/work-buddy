// Corpus manifest loader. The manifest lists every corpus doc by repo-relative
// read-only path with its expected SHA-256 and the schema-node extensions it
// requires (C1 surface contract section 7.2, adjudication 10). Each entry carries
// exactly the three frozen keys: path, expected_sha256, required_extensions.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { lf, sha256 } from "./text.js";

const SRC_DIR = path.dirname(fileURLToPath(import.meta.url));
/** The fidelity harness root: dashboard-react/tests/fidelity. */
export const FIDELITY_DIR = path.resolve(SRC_DIR, "..");
/** The repository root, three levels above the fidelity dir. */
export const REPO_ROOT = path.resolve(FIDELITY_DIR, "../../..");
/** The manifest file. */
export const MANIFEST_PATH = path.join(FIDELITY_DIR, "manifest.json");

/** One manifest entry: exactly the C1 frozen shape. */
export interface ManifestEntry {
  /** Repo-relative, read-only path to the corpus copy. */
  path: string;
  /** Lowercase hex SHA-256 of the LF-normalized file content. */
  expected_sha256: string;
  /** Optional (non-StarterKit) extensions the file needs as schema nodes. */
  required_extensions: string[];
}

export interface Manifest {
  schema_version: string;
  entries: ManifestEntry[];
}

/** Load and parse the corpus manifest. */
export function loadManifest(): Manifest {
  const raw = fs.readFileSync(MANIFEST_PATH, "utf8");
  return JSON.parse(raw) as Manifest;
}

/** Resolve a manifest entry path to an absolute path under the repo root. */
export function resolveEntryPath(entry: ManifestEntry): string {
  return path.join(REPO_ROOT, entry.path);
}

/** Read one corpus entry's LF-normalized source. */
export function readEntrySource(entry: ManifestEntry): string {
  return lf(fs.readFileSync(resolveEntryPath(entry), "utf8"));
}

export interface CorpusFile {
  entry: ManifestEntry;
  source: string;
  actual_sha256: string;
}

/** Read every corpus file named in the manifest, with its actual content hash. */
export function readCorpus(): CorpusFile[] {
  const manifest = loadManifest();
  return manifest.entries.map((entry) => {
    const source = readEntrySource(entry);
    return { entry, source, actual_sha256: sha256(source) };
  });
}
