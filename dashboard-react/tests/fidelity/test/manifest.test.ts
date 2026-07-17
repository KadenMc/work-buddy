// Manifest integrity. The corpus is a byte-fidelity fixture, so the suite fails
// hard if any corpus file drifts from its recorded hash, if the manifest shape
// deviates from the C1 frozen entry shape, or if a file requires a schema-node
// extension the frozen bundle does not carry.
import { describe, it, expect } from "vitest";
import { loadManifest, readCorpus } from "../src/corpus.js";
import { OPTIONAL_EXTENSION_IDS } from "../src/bundle.js";

const manifest = loadManifest();
const corpus = readCorpus();

describe("corpus manifest", () => {
  it("declares the versioned envelope", () => {
    expect(manifest.schema_version).toBe("wb-fidelity-corpus/v1");
    expect(Array.isArray(manifest.entries)).toBe(true);
  });

  it("carries at least 25 corpus docs (C1 requires 20-plus)", () => {
    expect(manifest.entries.length).toBeGreaterThanOrEqual(25);
  });

  it("gives every entry exactly the three frozen keys", () => {
    for (const entry of manifest.entries) {
      expect(Object.keys(entry).sort()).toEqual([
        "expected_sha256",
        "path",
        "required_extensions",
      ]);
      expect(typeof entry.path).toBe("string");
      expect(entry.path).toMatch(/^dashboard-react\/tests\/fidelity\/corpus\//);
      expect(entry.expected_sha256).toMatch(/^[0-9a-f]{64}$/);
      expect(Array.isArray(entry.required_extensions)).toBe(true);
    }
  });

  it("samples both real and synthetic construct files", () => {
    const paths = manifest.entries.map((entry) => entry.path);
    expect(paths.some((p) => p.includes("/corpus/real/"))).toBe(true);
    expect(paths.some((p) => p.includes("/corpus/synthetic/"))).toBe(true);
  });

  it("matches every corpus file against its recorded hash (fail-hard on drift)", () => {
    for (const file of corpus) {
      expect(
        file.actual_sha256,
        `hash drift in ${file.entry.path}`,
      ).toBe(file.entry.expected_sha256);
    }
  });

  it("requires only extensions the frozen bundle covers", () => {
    const supported = new Set<string>(OPTIONAL_EXTENSION_IDS);
    for (const entry of manifest.entries) {
      for (const ext of entry.required_extensions) {
        expect(
          supported.has(ext),
          `${entry.path} requires unsupported extension ${ext}`,
        ).toBe(true);
      }
    }
  });

  it("exercises table, task list, and image constructs somewhere in the corpus", () => {
    const union = new Set<string>();
    for (const entry of manifest.entries) {
      for (const ext of entry.required_extensions) union.add(ext);
    }
    expect(union.has("table")).toBe(true);
    expect(union.has("taskList")).toBe(true);
    expect(union.has("image")).toBe(true);
  });
});
