import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CoworkLedgerDecorations } from "../editor/ledgerDecorations";
import { InMemoryCoworkYdocTransport } from "../persistence/InMemoryCoworkYdocTransport";
import type { R2DocPayload } from "./types";
import { useCoworkBridge } from "./useCoworkBridge";

const headA = "a".repeat(64);
const headB = "b".repeat(64);

const provenancePayload = (head: string): R2DocPayload => {
  const record = {
    attestation_id: `attestation-${head[0]}`,
    at: "2026-08-12T12:00:00Z",
    asserted_by: { kind: "human", ref: "user-1", meta: null },
    scope: {
      kind: "document_version",
      document_version_id: `version-${head[0]}`,
      document_span_id: null,
      structured_head_sha256: head,
    },
    authorship: { kind: "ai", contributors: [] },
    human_review: { status: "not_reviewed", reviewers: [] },
    source: { kind: "generation", model: "test-model" },
    basis: { kind: "system_observation", ref: null },
    supersedes_id: null,
    canonical_sha256: "c".repeat(64),
  };
  return {
    document_id: "doc-1",
    store_id: "store-1",
    path: "docs/demo.md",
    title: "demo.md",
    profile: "co_authored",
    hashes: {
      ydoc_snapshot_sha256: null,
      last_materialized_sha256: null,
      current_file_sha256: null,
    },
    drift: { state: "clean", diff_available: false },
    open_proposals: [],
    expressions: [],
    provenance_spans: [],
    events_cursor: "cursor-1",
    provenance: {
      schema: "cowork-provenance-view/v1",
      current_structured_head_sha256: head,
      document_default: {
        projection_id: `document_version:version-${head[0]}`,
        target: {
          kind: "document_version",
          document_version_id: `version-${head[0]}`,
          document_span_id: null,
          structured_head_sha256: head,
          currentness: "current",
        },
        span: null,
        resolution: "resolved",
        review_eligibility: "eligible",
        issue: null,
        effective_attestation: record,
        effective_attestations: [record],
        history: [record],
      },
      spans: [],
      history: [record],
      summary: {
        total_targets: 1,
        current_span_count: 0,
        ai_unreviewed_count: 1,
        reviewed_count: 0,
        conflicted_count: 0,
        stale_count: 0,
        unrecorded: false,
      },
    },
  };
};

let editor: Editor | null = null;

afterEach(() => {
  editor?.destroy();
  editor = null;
});

describe("useCoworkBridge provenance freshness", () => {
  it("fails closed without an overlay when the initial provenance load rejects", async () => {
    const fetchDoc = vi.fn(async (): Promise<R2DocPayload> => {
      throw new Error("provenance transport unavailable");
    });
    const hook = renderHook(() =>
      useCoworkBridge({
        documentId: "doc-1",
        storeId: "store-1",
        docClient: { fetchDoc },
        ydocTransport: new InMemoryCoworkYdocTransport(),
      }),
    );
    editor = new Editor({
      element: document.createElement("div"),
      content: "<p>AI-authored text.</p>",
      extensions: [
        StarterKit.configure({ undoRedo: false }),
        CoworkLedgerDecorations,
      ],
    });

    act(() => {
      hook.result.current.editorProps.onReady({
        editor: editor!,
        dom: editor!.view.dom,
      });
      hook.result.current.setEditorLens("provenance");
    });
    await waitFor(() => expect(fetchDoc).toHaveBeenCalledTimes(1));
    await act(async () => {
      await Promise.resolve();
    });

    expect(hook.result.current.provenanceEditor.isLocallyDirty()).toBe(false);
    expect(
      editor.view.dom.querySelector("[data-wb-provenance-record-state]"),
    ).toBeNull();

    act(() => hook.result.current.editorProps.onTeardown());
    hook.unmount();
  });

  it("retains dirty settlement state when its authoritative refresh rejects", async () => {
    let requestCount = 0;
    const fetchDoc = vi.fn(async (): Promise<R2DocPayload> => {
      requestCount += 1;
      if (requestCount === 2) {
        throw new Error("settlement refresh unavailable");
      }
      return provenancePayload(requestCount === 1 ? headA : headB);
    });
    const hook = renderHook(() =>
      useCoworkBridge({
        documentId: "doc-1",
        storeId: "store-1",
        docClient: { fetchDoc },
        ydocTransport: new InMemoryCoworkYdocTransport(),
      }),
    );
    editor = new Editor({
      element: document.createElement("div"),
      content: "<p>AI-authored text.</p>",
      extensions: [
        StarterKit.configure({ undoRedo: false }),
        CoworkLedgerDecorations,
      ],
    });
    act(() => {
      hook.result.current.editorProps.onReady({
        editor: editor!,
        dom: editor!.view.dom,
      });
      hook.result.current.setEditorLens("provenance");
    });
    await waitFor(() =>
      expect(
        editor!.view.dom.querySelector(
          '[data-wb-provenance-record-state="recorded"]',
        ),
      ).not.toBeNull(),
    );

    act(() => hook.result.current.editorProps.onLocalProvenanceEdit?.());
    await act(async () => {
      hook.result.current.editorProps.onProvenancePersistenceSettled?.(headB);
      await Promise.resolve();
    });
    await waitFor(() => expect(fetchDoc).toHaveBeenCalledTimes(2));
    await act(async () => {
      await Promise.resolve();
    });

    expect(hook.result.current.provenanceEditor.isLocallyDirty()).toBe(true);
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-record-state="recorded"]',
      ),
    ).toBeNull();
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-record-state="unrecorded"]',
      ),
    ).not.toBeNull();

    // A later shared authoritative pull can settle the retained pending head;
    // the user does not need to repeat the persistence notification.
    await act(async () => {
      await hook.result.current.reviewProvider.load();
    });
    await waitFor(() =>
      expect(hook.result.current.provenanceEditor.isLocallyDirty()).toBe(false),
    );
    expect(fetchDoc).toHaveBeenCalledTimes(3);
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-record-state="recorded"]',
      ),
    ).not.toBeNull();

    act(() => hook.result.current.editorProps.onTeardown());
    hook.unmount();
  });

  it("restores a suppressed default only after persistence settlement and a fresh matching head", async () => {
    let authoritativeHead = headA;
    const fetchDoc = vi.fn(async () => provenancePayload(authoritativeHead));
    const hook = renderHook(() =>
      useCoworkBridge({
        documentId: "doc-1",
        storeId: "store-1",
        docClient: { fetchDoc },
        ydocTransport: new InMemoryCoworkYdocTransport(),
      }),
    );
    editor = new Editor({
      element: document.createElement("div"),
      content: "<p>AI-authored text.</p>",
      extensions: [
        StarterKit.configure({ undoRedo: false }),
        CoworkLedgerDecorations,
      ],
    });
    act(() => {
      hook.result.current.editorProps.onReady({
        editor: editor!,
        dom: editor!.view.dom,
      });
      hook.result.current.setEditorLens("provenance");
    });
    await waitFor(() =>
      expect(
        editor!.view.dom.querySelector(
          '[data-wb-provenance-record-state="recorded"]',
        ),
      ).not.toBeNull(),
    );

    act(() => hook.result.current.editorProps.onLocalProvenanceEdit?.());
    expect(hook.result.current.provenanceEditor.isLocallyDirty()).toBe(true);
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-record-state="recorded"]',
      ),
    ).toBeNull();
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-record-state="unrecorded"]',
      ),
    ).not.toBeNull();

    await act(async () => {
      hook.result.current.editorProps.onProvenancePersistenceSettled?.(headB);
      await Promise.resolve();
    });
    await waitFor(() => expect(fetchDoc).toHaveBeenCalledTimes(2));
    expect(hook.result.current.provenanceEditor.isLocallyDirty()).toBe(true);
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-record-state="recorded"]',
      ),
    ).toBeNull();

    authoritativeHead = headB;
    await act(async () => {
      hook.result.current.editorProps.onProvenancePersistenceSettled?.(headB);
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(hook.result.current.provenanceEditor.isLocallyDirty()).toBe(false),
    );
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-record-state="recorded"]',
      ),
    ).not.toBeNull();

    act(() => hook.result.current.editorProps.onTeardown());
    hook.unmount();
  });
});
