import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CoworkFolderSummary } from "../contracts";
import { sha256Hex } from "../persistence/hashing";
import { CoworkHttpClient } from "../providers/CoworkHttpClient";
import {
  CoworkDocumentLifecycleDialog,
  validRelativeMarkdownPath,
} from "./CoworkDocumentLifecycleDialog";

const folder: CoworkFolderSummary = {
  storeId: "store-1",
  folderName: "work-buddy",
  folderPath: "C:/Projects/work-buddy",
  layout: "wbuddy_cowork_v1",
  reachable: true,
  eligibility: "eligible",
  ineligibleReason: null,
  documentSurface: {
    enabled: true,
    allowedDocumentClasses: ["co_authored"],
    feedbackCapture: true,
  },
  permissions: {
    read: true,
    create: true,
    import: true,
    materialize: true,
    retire: true,
  },
  documentCount: 0,
};

describe("CoworkDocumentLifecycleDialog idempotent recovery", () => {
  it("reuses the operation key and opens a committed receipt after a lost commit response", async () => {
    const user = userEvent.setup();
    const idempotencyKeys: string[] = [];
    let committed = false;
    let sourceReads = 0;
    let commitCalls = 0;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/truth/doc/bootstrap?store_id=store-1") {
        const form = init?.body as FormData;
        const metadataPart = form.get("metadata");
        if (typeof metadataPart !== "string") throw new Error("missing metadata part");
        const metadataText = metadataPart;
        const metadata = JSON.parse(metadataText) as {
          idempotency_key: string;
        };
        idempotencyKeys.push(metadata.idempotency_key);
        const base = {
          ok: true,
          bootstrap_id: "bootstrap-1",
          document_id: "doc-1",
          mode: "create",
          normalized_path: "retry-document.md",
          source_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          source_byte_length: 0,
          source_url: "/bootstrap-1/source",
          ydoc_schema: "yjs-v1",
          expires_at: "2026-07-22T13:00:00.000Z",
        };
        return new Response(
          JSON.stringify(
            committed
              ? {
                  ...base,
                  state: "committed",
                  result: {
                    ok: true,
                    document_id: "doc-1",
                    path: "retry-document.md",
                    title: "Retry document",
                    document_class: "co_authored",
                    initialization_state: "ready",
                    snapshot_sha256: "a".repeat(64),
                    structured_head_sha256: "b".repeat(64),
                    projection_sha256: base.source_sha256,
                    current_file_sha256: base.source_sha256,
                    drift_state: "clean",
                  },
                }
              : { ...base, state: "prepared" },
          ),
          { status: committed ? 200 : 201, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/bootstrap-1/source") {
        sourceReads += 1;
        return new Response(new Uint8Array(0), { status: 200 });
      }
      if (url.startsWith("/api/truth/doc/bootstrap/bootstrap-1?")) {
        commitCalls += 1;
        committed = true;
        throw new TypeError("connection closed after commit");
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const onOpened = vi.fn(async () => undefined);
    const onClose = vi.fn();
    render(
      <CoworkDocumentLifecycleDialog
        mode="create"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        initialTitle="Retry document"
        onClose={onClose}
        onOpened={onOpened}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Create document" }));
    expect(await screen.findByRole("alert", undefined, { timeout: 10_000 })).toHaveTextContent(
      "connection closed after commit",
    );
    await user.click(screen.getByRole("button", { name: "Create document" }));

    await waitFor(() => expect(onOpened).toHaveBeenCalledTimes(1));
    expect(onOpened).toHaveBeenCalledWith(
      expect.objectContaining({ documentId: "doc-1", path: "retry-document.md" }),
    );
    expect(idempotencyKeys).toHaveLength(2);
    expect(idempotencyKeys[1]).toBe(idempotencyKeys[0]);
    expect(sourceReads).toBe(1);
    expect(commitCalls).toBe(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  }, 20_000);

  it("repairs the existing document from exact staged Markdown without a file rewrite", async () => {
    const user = userEvent.setup();
    const source = new TextEncoder().encode("# Repaired from Markdown\n");
    const sourceSha = await sha256Hex(source);
    const requests: string[] = [];
    const repairDocument = {
      documentId: "doc-repair",
      path: "Docs/My Working Note.MD",
      title: "My Working Note",
      profile: "co_authored",
      initializationState: "semantic_corrupt" as const,
      driftState: "clean" as const,
      currentFileSha256: sourceSha,
      openProposalCount: 0,
      openFlagCount: 0,
      permissions: {
        open: false,
        edit: false,
        materialize: false,
        repair: true,
        retire: true,
      },
    };
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push(url);
      if (url === "/api/truth/doc/bootstrap?store_id=store-1") {
        const form = init?.body as FormData;
        const metadataPart = form.get("metadata");
        if (typeof metadataPart !== "string") throw new Error("missing metadata");
        const metadataText = metadataPart;
        expect(JSON.parse(metadataText)).toMatchObject({
          mode: "repair",
          path: repairDocument.path,
          title: repairDocument.title,
          expected_file_sha256: sourceSha,
          document_id: repairDocument.documentId,
        });
        expect(form.get("source")).toBeNull();
        return new Response(
          JSON.stringify({
            bootstrap_id: "bootstrap-repair",
            document_id: repairDocument.documentId,
            mode: "repair",
            normalized_path: repairDocument.path,
            source_sha256: sourceSha,
            source_byte_length: source.byteLength,
            source_url: "/bootstrap-repair/source",
            ydoc_schema: "yjs-v1",
            expires_at: "2026-07-22T20:00:00Z",
            state: "prepared",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/bootstrap-repair/source") {
        return new Response(source as BodyInit, { status: 200 });
      }
      if (url.startsWith("/api/truth/doc/bootstrap/bootstrap-repair?")) {
        expect(init?.method).toBe("PUT");
        return new Response(
          JSON.stringify({
            document_id: repairDocument.documentId,
            path: repairDocument.path,
            title: repairDocument.title,
            document_class: "co_authored",
            initialization_state: "ready",
            snapshot_sha256: "a".repeat(64),
            structured_head_sha256: "b".repeat(64),
            projection_sha256: sourceSha,
            current_file_sha256: sourceSha,
            drift_state: "clean",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const onOpened = vi.fn(async () => undefined);
    render(
      <CoworkDocumentLifecycleDialog
        mode="repair"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        repairDocument={repairDocument}
        onClose={vi.fn()}
        onOpened={onOpened}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Repair document" }));
    await waitFor(() => expect(onOpened).toHaveBeenCalledTimes(1));
    expect(onOpened).toHaveBeenCalledWith(
      expect.objectContaining({
        documentId: repairDocument.documentId,
        initializationState: "ready",
      }),
    );
    expect(requests).toEqual([
      "/api/truth/doc/bootstrap?store_id=store-1",
      "/bootstrap-repair/source",
      "/api/truth/doc/bootstrap/bootstrap-repair?store_id=store-1",
    ]);
  }, 20_000);

  it.each([
    "notes/report:secret.md",
    "notes/bad?.md",
    "notes/CON.md",
    "notes/LPT1.markdown",
    "folder./note.md",
    "folder /note.md",
  ])("rejects unsafe Windows path %s before any prepare request", async (unsafePath) => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn(async () => {
      throw new Error("prepare must not be called for an unsafe path");
    });
    render(
      <CoworkDocumentLifecycleDialog
        mode="create"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        onClose={vi.fn()}
        onOpened={vi.fn()}
      />,
    );
    await user.type(screen.getByRole("textbox", { name: "Title" }), "Safe title");
    const path = screen.getByRole("textbox", { name: /Location inside/ });
    await user.clear(path);
    await user.type(path, unsafePath);
    await user.click(screen.getByRole("button", { name: "Create document" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Use a safe relative .md or .markdown location",
    );
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("accepts ordinary spaces and mixed case while still remaining relative", () => {
    expect(validRelativeMarkdownPath("Docs/My Working Note.MD")).toBe(true);
    expect(validRelativeMarkdownPath("../escape.md")).toBe(false);
    expect(validRelativeMarkdownPath("C:/absolute.md")).toBe(false);
  });
});
