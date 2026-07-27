import { Editor } from "@tiptap/core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import * as Y from "yjs";

import type { CoworkDocumentSummary } from "../contracts";
import { buildEditorExtensions } from "../editor/extensions";
import { serializeCoworkEditorMarkdown } from "../editor/serializeCoworkMarkdown";
import { sha256Hex } from "../persistence/hashing";
import { CoworkHttpClient } from "../providers/CoworkHttpClient";
import { CoworkReimportDialog } from "./CoworkReimportDialog";

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const readBlobBytes = (blob: Blob): Promise<Uint8Array> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error ?? new Error("Could not read Blob"));
    reader.readAsArrayBuffer(blob);
  });

describe("CoworkReimportDialog", () => {
  it("shows an exact redline, confirms the server consequence, and commits only external content", async () => {
    const user = userEvent.setup();
    const baseline = new TextEncoder().encode("# Old-only heading\n\nOld-only body.\n");
    const external = new TextEncoder().encode(
      "# External-only heading\n\nExternal-only body.\n",
    );
    const baselineSha = await sha256Hex(baseline);
    const externalSha = await sha256Hex(external);
    const document: CoworkDocumentSummary = {
      documentId: "doc-1",
      path: "notes/working.md",
      title: "Working note",
      profile: "co_authored",
      driftState: "drifted",
      openProposalCount: 0,
      openFlagCount: 0,
      snapshotSha256: "a".repeat(64),
      structuredHeadSha256: "b".repeat(64),
      projectionSha256: baselineSha,
      currentFileSha256: externalSha,
      permissions: {
        open: true,
        edit: true,
        materialize: true,
        repair: true,
        retire: true,
      },
    };
    let preparedKey = "";
    let committedMarkdown = "";
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      if (url.endsWith("/drift?store_id=store-1")) {
        return json({
          state: "drifted",
          last_materialized_sha256: baselineSha,
          current_file_sha256: externalSha,
          snapshot_sha256: document.snapshotSha256,
          structured_head_sha256: document.structuredHeadSha256,
          update_tail_present: false,
          unmaterialized_structured_edits: false,
          diff_available: true,
          can_reimport: true,
          baseline: {
            available: true,
            sha256: baselineSha,
            etag: '"baseline-v1"',
            source_url: "/baseline",
          },
          source: {
            available: true,
            sha256: externalSha,
            etag: '"source-v2"',
            source_url: "/external",
          },
        });
      }
      if (url === "/baseline") {
        expect(init.headers).toMatchObject({ "If-Match": '"baseline-v1"' });
        return new Response(baseline as BodyInit, {
          status: 200,
          headers: { ETag: '"baseline-v1"' },
        });
      }
      if (url === "/external") {
        expect(init.headers).toMatchObject({ "If-Match": '"source-v2"' });
        return new Response(external as BodyInit, {
          status: 200,
          headers: { ETag: '"source-v2"' },
        });
      }
      if (url.endsWith("/reimport?store_id=store-1")) {
        preparedKey = (JSON.parse(String(init.body)) as { idempotency_key: string })
          .idempotency_key;
        return json({
          intent_id: "reimport-1",
          state: "prepared",
          expires_at: "2026-07-22T20:00:00Z",
          source_sha256: externalSha,
          source_byte_length: external.byteLength,
          prior_projection_sha256: baselineSha,
          prior_snapshot_sha256: document.snapshotSha256,
          prior_structured_head_sha256: document.structuredHeadSha256,
          consequence:
            "Replace the structured document from external Markdown and stale one proposal.",
        });
      }
      if (url.endsWith("/reimport/reimport-1/source?store_id=store-1")) {
        return new Response(external as BodyInit, { status: 200 });
      }
      if (url.endsWith("/reimport/reimport-1/commit?store_id=store-1")) {
        const form = init.body as FormData;
        const snapshotPart = form.get("snapshot");
        expect(snapshotPart).toBeInstanceOf(Blob);
        const snapshot = await readBlobBytes(snapshotPart as Blob);
        const replacement = new Y.Doc();
        Y.applyUpdate(replacement, snapshot);
        const editor = new Editor({ extensions: buildEditorExtensions(replacement) });
        committedMarkdown = serializeCoworkEditorMarkdown(editor, replacement);
        editor.destroy();
        replacement.destroy();
        return json({
          intent_id: "reimport-1",
          document_id: "doc-1",
          source_sha256: externalSha,
          snapshot_sha256: "c".repeat(64),
          structured_head_sha256: "d".repeat(64),
          document_version_id: "version-2",
          doc_event_id: "event-2",
          staled_proposal_ids: ["proposal-1"],
          reimported_at: "2026-07-22T19:00:00Z",
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const onReimported = vi.fn(async () => undefined);
    render(
      <CoworkReimportDialog
        storeId="store-1"
        document={document}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        onClose={vi.fn()}
        onReimported={onReimported}
      />,
    );

    const redline = await screen.findByLabelText("Markdown changes");
    expect(redline).toHaveTextContent("- # Old-only heading");
    expect(redline).toHaveTextContent("+ # External-only heading");
    await user.click(screen.getByRole("button", { name: "Continue to replacement" }));
    expect(await screen.findByText(/Replace the structured document/)).toBeVisible();
    expect(preparedKey).not.toBe("");
    await user.click(screen.getByRole("button", { name: "Replace Co-work document" }));

    await waitFor(() => expect(onReimported).toHaveBeenCalledTimes(1));
    expect(committedMarkdown).toBe(new TextDecoder().decode(external));
    expect(committedMarkdown).toContain("External-only");
    expect(committedMarkdown).not.toContain("Old-only");
  }, 20_000);

  it("retries only local session reconciliation after the server commit succeeds", async () => {
    const user = userEvent.setup();
    const external = new TextEncoder().encode("# Replacement\n");
    const externalSha = await sha256Hex(external);
    const document: CoworkDocumentSummary = {
      documentId: "doc-reconcile",
      path: "notes/reconcile.md",
      title: "Reconcile",
      profile: "co_authored",
      driftState: "drifted",
      openProposalCount: 0,
      openFlagCount: 0,
      snapshotSha256: "a".repeat(64),
      structuredHeadSha256: "b".repeat(64),
      projectionSha256: "c".repeat(64),
      currentFileSha256: externalSha,
      permissions: { open: true, edit: true, materialize: true, repair: true, retire: true },
    };
    let sourceReads = 0;
    let commits = 0;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/drift?store_id=store-1")) {
        return json({
          state: "drifted",
          last_materialized_sha256: document.projectionSha256,
          current_file_sha256: externalSha,
          snapshot_sha256: document.snapshotSha256,
          structured_head_sha256: document.structuredHeadSha256,
          update_tail_present: false,
          unmaterialized_structured_edits: false,
          diff_available: false,
          can_reimport: true,
          baseline: { available: false, sha256: null, etag: null, source_url: null },
          source: { available: false, sha256: externalSha, etag: null, source_url: null },
        });
      }
      if (url.endsWith("/reimport?store_id=store-1")) {
        return json({
          intent_id: "reimport-reconcile",
          state: "prepared",
          expires_at: "2026-07-22T20:00:00Z",
          source_sha256: externalSha,
          source_byte_length: external.byteLength,
          prior_projection_sha256: document.projectionSha256,
          prior_snapshot_sha256: document.snapshotSha256,
          prior_structured_head_sha256: document.structuredHeadSha256,
          consequence: "Replace the structured document.",
        });
      }
      if (url.endsWith("/reimport/reimport-reconcile/source?store_id=store-1")) {
        sourceReads += 1;
        return new Response(external as BodyInit, { status: 200 });
      }
      if (url.endsWith("/reimport/reimport-reconcile/commit?store_id=store-1")) {
        commits += 1;
        return json({
          intent_id: "reimport-reconcile",
          document_id: document.documentId,
          source_sha256: externalSha,
          snapshot_sha256: "d".repeat(64),
          structured_head_sha256: "e".repeat(64),
          document_version_id: "version-reconcile",
          doc_event_id: "event-reconcile",
          staled_proposal_ids: [],
          reimported_at: "2026-07-22T19:00:00Z",
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const onReimported = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error("catalog refresh failed"))
      .mockResolvedValue(undefined);
    render(
      <CoworkReimportDialog
        storeId="store-1"
        document={document}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        onClose={vi.fn()}
        onReimported={onReimported}
      />,
    );

    await user.click(
      await screen.findByRole("button", { name: "Continue to replacement" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Replace Co-work document" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("catalog refresh failed");
    await user.click(screen.getByRole("button", { name: "Retry replacement" }));
    await waitFor(() => expect(onReimported).toHaveBeenCalledTimes(2));

    expect(commits).toBe(1);
    expect(sourceReads).toBe(1);
  }, 20_000);

  it("replays the exact idempotent commit when its successful response is lost", async () => {
    const user = userEvent.setup();
    const external = new TextEncoder().encode("# Response-lost replacement\n");
    const externalSha = await sha256Hex(external);
    const document: CoworkDocumentSummary = {
      documentId: "doc-response-lost",
      path: "notes/response-lost.md",
      title: "Response lost",
      profile: "co_authored",
      driftState: "drifted",
      openProposalCount: 0,
      openFlagCount: 0,
      snapshotSha256: "a".repeat(64),
      structuredHeadSha256: "b".repeat(64),
      projectionSha256: "c".repeat(64),
      currentFileSha256: externalSha,
      permissions: { open: true, edit: true, materialize: true, repair: true, retire: true },
    };
    let sourceReads = 0;
    let commits = 0;
    const receipt = {
      intent_id: "reimport-response-lost",
      document_id: document.documentId,
      source_sha256: externalSha,
      snapshot_sha256: "d".repeat(64),
      structured_head_sha256: "e".repeat(64),
      document_version_id: "version-response-lost",
      doc_event_id: "event-response-lost",
      staled_proposal_ids: [],
      reimported_at: "2026-07-22T19:00:00Z",
    };
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/drift?store_id=store-1")) {
        return json({
          state: "drifted",
          last_materialized_sha256: document.projectionSha256,
          current_file_sha256: externalSha,
          snapshot_sha256: document.snapshotSha256,
          structured_head_sha256: document.structuredHeadSha256,
          update_tail_present: false,
          unmaterialized_structured_edits: false,
          diff_available: false,
          can_reimport: true,
          baseline: { available: false, sha256: null, etag: null, source_url: null },
          source: { available: false, sha256: externalSha, etag: null, source_url: null },
        });
      }
      if (url.endsWith("/reimport?store_id=store-1")) {
        return json({
          intent_id: receipt.intent_id,
          state: "prepared",
          expires_at: "2026-07-22T20:00:00Z",
          source_sha256: externalSha,
          source_byte_length: external.byteLength,
          prior_projection_sha256: document.projectionSha256,
          prior_snapshot_sha256: document.snapshotSha256,
          prior_structured_head_sha256: document.structuredHeadSha256,
          consequence: "Replace the structured document.",
        });
      }
      if (url.endsWith(`/reimport/${receipt.intent_id}/source?store_id=store-1`)) {
        sourceReads += 1;
        if (sourceReads > 1) {
          return json(
            {
              ok: false,
              error: {
                code: "staged_source_missing",
                message: "The committed source is no longer staged.",
                retryable: false,
              },
            },
            409,
          );
        }
        return new Response(external as BodyInit, { status: 200 });
      }
      if (url.endsWith(`/reimport/${receipt.intent_id}/commit?store_id=store-1`)) {
        commits += 1;
        if (commits === 1) throw new TypeError("Failed to fetch");
        return json(receipt);
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const onReimported = vi.fn(async () => undefined);
    render(
      <CoworkReimportDialog
        storeId="store-1"
        document={document}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        onClose={vi.fn()}
        onReimported={onReimported}
      />,
    );

    await user.click(
      await screen.findByRole("button", { name: "Continue to replacement" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Replace Co-work document" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to fetch");
    await user.click(screen.getByRole("button", { name: "Retry replacement" }));

    await waitFor(() => expect(onReimported).toHaveBeenCalledTimes(1));
    expect(commits).toBe(2);
    expect(sourceReads).toBe(1);
  }, 20_000);
});
