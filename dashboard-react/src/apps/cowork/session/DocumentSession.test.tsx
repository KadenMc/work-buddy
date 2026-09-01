import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CoworkBridge } from "../bridge";
import {
  DocumentSessionProvider,
  DocumentSessionRegistry,
  DuplicateWritableDocumentSessionError,
  documentSessionKey,
  useDocumentSessionContext,
  type DocumentSession,
} from "./DocumentSession";

const session = (): DocumentSession => ({
  key: documentSessionKey({ storeId: "store-1", documentId: "doc-1" }),
  reference: { kind: "workspace", storeId: "store-1", documentId: "doc-1" },
  bridge: {} as CoworkBridge,
  writable: true,
  syncStatus: "clean",
});
function Probe() {
  const current = useDocumentSessionContext();
  return <output>{current.key}</output>;
}

describe("DocumentSession", () => {
  it("shares the exact session through nested presentation hosts", () => {
    const shared = session();
    render(
      <DocumentSessionProvider session={shared}>
        <DocumentSessionProvider session={shared}>
          <Probe />
        </DocumentSessionProvider>
      </DocumentSessionProvider>,
    );
    expect(screen.getByRole("status")).toHaveTextContent(shared.key);
  });

  it("permits one writable runtime per document identity", () => {
    const registry = new DocumentSessionRegistry();
    const key = documentSessionKey({ storeId: "store-1", documentId: "doc-1" });
    const release = registry.register(key, "full-workspace", true);
    expect(registry.hasWritable(key)).toBe(true);
    expect(() => registry.register(key, "contextual-panel", true)).toThrow(
      DuplicateWritableDocumentSessionError,
    );

    // Read-only inspection is not another writable bridge.
    expect(() => registry.register(key, "read-only-preview", false)).not.toThrow();
    release();
    expect(registry.hasWritable(key)).toBe(false);
    expect(() => registry.register(key, "contextual-panel", true)).not.toThrow();
  });

  it("keeps idempotent registrations alive until every lease is released", () => {
    const registry = new DocumentSessionRegistry();
    const first = registry.register("identity", "host", true);
    const second = registry.register("identity", "host", true);
    first();
    expect(registry.hasWritable("identity")).toBe(true);
    second();
    expect(registry.hasWritable("identity")).toBe(false);
  });
});
