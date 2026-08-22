import { describe, expect, it, vi } from "vitest";

vi.mock("../../../security/humanAuthority", () => ({
  coworkHumanAuthorityHeaders: vi.fn(async () => ({})),
}));

import type { CoworkPasteProvenanceRequest } from "../provenance";
import {
  COWORK_FOLDER_PICKER_INTENT,
  COWORK_FOLDER_PICKER_INTENT_HEADER,
  COWORK_IMPORT_PICKER_INTENT,
  COWORK_LOCATION_PICKER_INTENT,
  CoworkHttpClient,
  normalizeDocumentSummary,
} from "./CoworkHttpClient";

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const sourceSha256 = "a".repeat(64);
const markdownImporterWire = {
  importer_id: "markdown/v1",
  display_name: "Markdown",
  source_format: "markdown",
  media_type: "text/markdown",
  suffixes: [".md", ".markdown"],
  max_source_bytes: 16 * 1024 * 1024,
} as const;
const markdownImporter = {
  importerId: "markdown/v1",
  displayName: "Markdown",
  sourceFormat: "markdown",
  mediaType: "text/markdown",
  suffixes: [".md", ".markdown"],
  maxSourceBytes: 16 * 1024 * 1024,
} as const;

const pasteProvenanceRequest: CoworkPasteProvenanceRequest = {
  storeId: "store with space",
  documentId: "doc/with slash",
  sourceKind: "paste",
  basisKind: "user_attestation",
  expectedStructuredHeadSha256: "a".repeat(64),
  anchor: {
    exact: "Pasted text",
    prefix: "Before ",
    suffix: " after",
  },
  attestation: {
    schema: "cowork-authorship-attestation/v1",
    authorship: {
      kind: "ai",
      contributors: [],
    },
    human_review: {
      status: "reviewed",
      reviewers: [
        {
          kind: "current_user",
          ref: "dashboard-user",
          identity_status: "local_actor_ref",
        },
      ],
    },
  },
  idempotencyKey: "paste-provenance-key",
};

describe("CoworkHttpClient document lifecycle contracts", () => {
  it("loads the immutable current actor identity used by provenance capture", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      expect(String(input)).toBe("/api/truth/cowork/current-actor");
      return json({
        kind: "human",
        ref: "account-user-17",
        identity_status: "account_ref",
      });
    });

    await expect(
      new CoworkHttpClient(fetchImpl as typeof fetch).currentActor(),
    ).resolves.toEqual({
      kind: "human",
      ref: "account-user-17",
      identity_status: "account_ref",
    });
  });

  it("fails closed when the current actor response is not identity-bound", async () => {
    const client = new CoworkHttpClient(
      vi.fn(async () =>
        json({ kind: "human", identity_status: "local_actor_ref" }),
      ) as typeof fetch,
    );

    await expect(client.currentActor()).rejects.toMatchObject({
      apiError: {
        code: "invalid_current_actor_response",
      },
    });
  });

  it("records exact pasted-span provenance through the document-scoped API", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      expect(String(input)).toBe(
        "/api/truth/doc/doc%2Fwith%20slash/authorship-attestations?store_id=store%20with%20space",
      );
      expect(init.method).toBe("POST");
      expect(init.credentials).toBe("same-origin");
      expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
      expect(JSON.parse(String(init.body))).toEqual({
        span: pasteProvenanceRequest.anchor,
        attestation: pasteProvenanceRequest.attestation,
        source_kind: "paste",
        basis_kind: "user_attestation",
        expected_structured_head_sha256: "a".repeat(64),
        idempotency_key: "paste-provenance-key",
      });
      return json({
        attestation_id: "attestation-1",
        document_span_id: "span-1",
        target_structured_head_sha256: "a".repeat(64),
      });
    });
    const client = new CoworkHttpClient(fetchImpl as typeof fetch);

    await expect(client.recordPasteProvenance(pasteProvenanceRequest)).resolves.toEqual({
      attestationId: "attestation-1",
      documentSpanId: "span-1",
      targetStructuredHeadSha256: "a".repeat(64),
    });
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it("preserves a stale structured-head API error for a paste provenance retry", async () => {
    const fetchImpl = vi.fn(async () =>
      json(
        {
          error: {
            code: "stale_structured_head",
            message: "The document changed before this provenance was recorded.",
            retryable: true,
          },
        },
        409,
      ),
    );
    const client = new CoworkHttpClient(fetchImpl as typeof fetch);

    await expect(client.recordPasteProvenance(pasteProvenanceRequest)).rejects.toMatchObject({
      apiError: {
        code: "stale_structured_head",
        retryable: true,
        status: 409,
      },
    });
  });

  it("rejects a provenance receipt for a different structured document head", async () => {
    const fetchImpl = vi.fn(async () =>
      json({
        attestation_id: "attestation-1",
        document_span_id: "span-1",
        target_structured_head_sha256: "b".repeat(64),
      }),
    );
    const client = new CoworkHttpClient(fetchImpl as typeof fetch);

    await expect(client.recordPasteProvenance(pasteProvenanceRequest)).rejects.toMatchObject({
      apiError: {
        code: "invalid_provenance_response",
        retryable: true,
      },
    });
  });

  it("preserves picker-specific availability from Folder discovery", async () => {
    const fetchImpl = vi.fn(async () =>
      json({
        read_only: false,
        folders: [],
        diagnostics: [],
        chooser: {
          available: true,
          kind: "host_native",
          import_available: false,
          markdown_available: true,
          location_available: true,
        },
      }),
    );
    const client = new CoworkHttpClient(fetchImpl as typeof fetch);

    await expect(client.listFolders()).resolves.toMatchObject({
      chooser: {
        available: true,
        kind: "host_native",
        importAvailable: false,
        locationAvailable: true,
      },
    });
  });

  it("falls back to legacy Markdown picker availability when import availability is absent", async () => {
    const fetchImpl = vi.fn(async () =>
      json({
        read_only: false,
        folders: [],
        diagnostics: [],
        chooser: {
          available: true,
          kind: "legacy_host",
          markdown_available: false,
          location_available: true,
        },
      }),
    );
    const client = new CoworkHttpClient(fetchImpl as typeof fetch);

    await expect(client.listFolders()).resolves.toMatchObject({
      chooser: {
        importAvailable: false,
      },
    });
  });

  it("marks Folder picker requests with an explicit local user-intent header", async () => {
    const fetchImpl = vi.fn(
      async (_input: RequestInfo | URL, _init: RequestInit = {}) =>
        json({ cancelled: true }),
    );
    const client = new CoworkHttpClient(fetchImpl as typeof fetch);

    await expect(client.chooseFolder()).resolves.toMatchObject({ cancelled: true });

    expect(fetchImpl).toHaveBeenCalledOnce();
    const [url, requestInit] = fetchImpl.mock.calls[0];
    const init = requestInit ?? {};
    const headers = new Headers(init.headers);
    expect(String(url)).toBe("/api/truth/cowork/folders/choose");
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      body: "{}",
    });
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get(COWORK_FOLDER_PICKER_INTENT_HEADER)).toBe(
      COWORK_FOLDER_PICKER_INTENT,
    );
  });

  it.each([
    [
      "file import",
      "chooseImportFile",
      "/api/truth/cowork/files/choose-import",
      COWORK_IMPORT_PICKER_INTENT,
      "notes/source.md",
    ],
    [
      "location",
      "chooseLocation",
      "/api/truth/cowork/folders/choose-location",
      COWORK_LOCATION_PICKER_INTENT,
      "",
    ],
  ] as const)(
    "marks %s picker requests with store context and preserves an empty root path",
    async (_label, method, expectedUrl, expectedIntent, path) => {
      const fetchImpl = vi.fn(
        async (_input: RequestInfo | URL, _init: RequestInit = {}) =>
          json(
            method === "chooseImportFile"
              ? {
                  cancelled: false,
                  path,
                  importer_id: "markdown/v1",
                  media_type: "text/markdown",
                  source_sha256: sourceSha256,
                  importer: markdownImporterWire,
                }
              : { cancelled: false, path },
          ),
      );
      const client = new CoworkHttpClient(fetchImpl as typeof fetch);

      await expect(client[method]("store-1")).resolves.toEqual(
        method === "chooseImportFile"
          ? {
              cancelled: false,
              path,
              importerId: "markdown/v1",
              mediaType: "text/markdown",
              sourceSha256,
              importer: markdownImporter,
            }
          : { cancelled: false, path },
      );

      const [url, init = {}] = fetchImpl.mock.calls[0];
      const headers = new Headers(init.headers);
      expect(String(url)).toBe(expectedUrl);
      expect(init.method).toBe("POST");
      expect(JSON.parse(String(init.body))).toEqual({ store_id: "store-1" });
      expect(headers.get(COWORK_FOLDER_PICKER_INTENT_HEADER)).toBe(expectedIntent);
    },
  );

  it.each(["chooseImportFile", "chooseLocation"] as const)(
    "rejects a successful %s response that omits its required path",
    async (method) => {
      const fetchImpl = vi.fn(
        async (_input: RequestInfo | URL, _init: RequestInit = {}) =>
          json({ cancelled: false }),
      );
      const client = new CoworkHttpClient(fetchImpl as typeof fetch);

      await expect(client[method]("store-1")).rejects.toMatchObject({
        apiError: {
          code: "invalid_picker_response",
          retryable: true,
        },
      });
    },
  );

  it.each([
    {
      cancelled: false,
      path: "notes/source.md",
      media_type: "text/markdown",
      source_sha256: sourceSha256,
      importer: markdownImporterWire,
    },
    {
      cancelled: false,
      path: "notes/source.md",
      importer_id: "markdown/v1",
      source_sha256: sourceSha256,
      importer: markdownImporterWire,
    },
    {
      cancelled: false,
      path: "notes/source.md",
      importer_id: "markdown/v1",
      media_type: "text/markdown",
      importer: markdownImporterWire,
    },
  ])(
    "rejects a non-cancelled file selection without a complete importer identity",
    async (payload) => {
      const fetchImpl = vi.fn(async () => json(payload));
      const client = new CoworkHttpClient(fetchImpl as typeof fetch);

      await expect(client.chooseImportFile("store-1")).rejects.toMatchObject({
        apiError: {
          code: "invalid_picker_response",
          retryable: true,
        },
      });
    },
  );

  it.each([
    undefined,
    {
      ...markdownImporterWire,
      importer_id: "markdown/v2",
    },
    {
      ...markdownImporterWire,
      suffixes: [],
    },
    {
      ...markdownImporterWire,
      max_source_bytes: 0,
    },
  ])(
    "rejects a malformed or mismatched server importer descriptor",
    async (importer) => {
      const fetchImpl = vi.fn(async () =>
        json({
          cancelled: false,
          path: "notes/source.md",
          importer_id: "markdown/v1",
          media_type: "text/markdown",
          source_sha256: sourceSha256,
          importer,
        }),
      );
      const client = new CoworkHttpClient(fetchImpl as typeof fetch);

      await expect(client.chooseImportFile("store-1")).rejects.toMatchObject({
        apiError: {
          code: "invalid_picker_response",
          retryable: true,
        },
      });
    },
  );

  it("falls back to the path basename when the server title is blank", () => {
    expect(
      normalizeDocumentSummary({
        document_id: "doc-1",
        path: "Docs/My Working Note.MD",
        title: "   ",
      }).title,
    ).toBe("My Working Note.MD");
  });

  it("preserves detached import source identity and the currently observed source hash", () => {
    expect(
      normalizeDocumentSummary({
        document_id: "detached",
        path: "drafts/source.md",
        source_writeback: "never",
        import_source_sha256: "a".repeat(64),
        observed_source_file_sha256: "b".repeat(64),
      }),
    ).toMatchObject({
      importSourceSha256: "a".repeat(64),
      observedSourceFileSha256: "b".repeat(64),
    });
    expect(
      normalizeDocumentSummary({
        document_id: "detached-nested",
        path: "drafts/source.md",
        hashes: {
          import_source_sha256: "c".repeat(64),
          observed_source_file_sha256: "d".repeat(64),
        },
      }),
    ).toMatchObject({
      importSourceSha256: "c".repeat(64),
      observedSourceFileSha256: "d".repeat(64),
    });
  });

  it("keeps absent legacy writeback metadata compatible but fails closed once the field is supplied", () => {
    expect(
      normalizeDocumentSummary({
        document_id: "legacy",
        path: "legacy.md",
      }).sourceWriteback,
    ).toBe("same_file");
    expect(
      normalizeDocumentSummary({
        document_id: "current",
        path: "current.md",
        source_writeback: "same_file",
      }).sourceWriteback,
    ).toBe("same_file");

    for (const sourceWriteback of ["future_policy", null, false, ""]) {
      expect(
        normalizeDocumentSummary({
          document_id: "malformed",
          path: "malformed.md",
          source_writeback: sourceWriteback,
        }).sourceWriteback,
      ).toBe("never");
    }
    expect(
      normalizeDocumentSummary({
        document_id: "malformed-camel",
        path: "malformed-camel.md",
        sourceWriteback: "future_policy",
      }).sourceWriteback,
    ).toBe("never");
  });

  it("uses prepare, exact staged source, and multipart commit for re-import", async () => {
    const requests: Array<{ readonly url: string; readonly init: RequestInit }> = [];
    const source = new TextEncoder().encode("# External source\n");
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      requests.push({ url, init });
      if (url.endsWith("/reimport?store_id=store-1")) {
        expect(init.method).toBe("POST");
        expect(init.credentials).toBe("same-origin");
        expect(JSON.parse(String(init.body))).toEqual({ idempotency_key: "stable-key" });
        return json({
          intent_id: "reimport-1",
          state: "prepared",
          expires_at: "2026-07-22T20:00:00Z",
          source_sha256: "a".repeat(64),
          source_byte_length: source.byteLength,
          prior_projection_sha256: "b".repeat(64),
          prior_snapshot_sha256: "c".repeat(64),
          prior_structured_head_sha256: "d".repeat(64),
          consequence: "Replace structured state from the staged Markdown.",
        });
      }
      if (url.endsWith("/reimport/reimport-1/source?store_id=store-1")) {
        expect(init).toMatchObject({ method: "GET", credentials: "same-origin" });
        return new Response(source, { status: 200 });
      }
      if (url.endsWith("/reimport/reimport-1/commit?store_id=store-1")) {
        expect(init.method).toBe("PUT");
        expect(init.credentials).toBe("same-origin");
        const form = init.body as FormData;
        const metadata = form.get("metadata");
        const snapshot = form.get("snapshot");
        expect(typeof metadata).toBe("string");
        expect(snapshot).toBeInstanceOf(Blob);
        expect(JSON.parse(String(metadata))).toEqual({
          snapshot_sha256: "e".repeat(64),
        });
        return json({
          intent_id: "reimport-1",
          document_id: "doc-1",
          source_sha256: "a".repeat(64),
          snapshot_sha256: "e".repeat(64),
          structured_head_sha256: "f".repeat(64),
          document_version_id: "version-2",
          doc_event_id: "event-2",
          staled_proposal_ids: ["proposal-1"],
          reimported_at: "2026-07-22T19:00:00Z",
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const client = new CoworkHttpClient(fetchImpl as typeof fetch);

    const prepared = await client.prepareReimport("store-1", "doc-1", "stable-key");
    expect(prepared).toMatchObject({ intentId: "reimport-1", state: "prepared" });
    expect(
      Array.from(
        await client.readReimportSource("store-1", "doc-1", prepared.intentId),
      ),
    ).toEqual(Array.from(source));
    const receipt = await client.commitReimport(
      "store-1",
      "doc-1",
      prepared,
      new Uint8Array([1, 2, 3]),
      "e".repeat(64),
    );
    expect(receipt).toMatchObject({
      documentId: "doc-1",
      documentVersionId: "version-2",
      staledProposalIds: ["proposal-1"],
    });
    expect(requests).toHaveLength(3);
  });

  it("uses the same retirement route for server-prepared consequence and commit", async () => {
    const bodies: unknown[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      expect(String(input)).toBe("/api/truth/doc/doc-1/retire?store_id=store-1");
      expect(init).toMatchObject({ method: "POST", credentials: "same-origin" });
      const body = JSON.parse(String(init.body));
      bodies.push(body);
      if ("idempotency_key" in body) {
        return json({
          intent_id: "retire-1",
          expires_at: "2026-07-22T20:00:00Z",
          document_id: "doc-1",
          consequence: "The Markdown file and history are retained.",
          consequence_sha256: "a".repeat(64),
        });
      }
      return json({
        intent_id: "retire-1",
        document_id: "doc-1",
        lifecycle: "retired",
        retired_at: "2026-07-22T19:00:00Z",
        doc_event_id: "event-retired",
        file_retained: true,
        history_retained: true,
      });
    });
    const client = new CoworkHttpClient(fetchImpl as typeof fetch);

    const prepared = await client.prepareRetirement("store-1", "doc-1", "retire-key");
    expect(prepared.consequence).toContain("file and history are retained");
    const receipt = await client.commitRetirement(
      "store-1",
      "doc-1",
      prepared.intentId,
    );
    expect(receipt).toMatchObject({
      lifecycle: "retired",
      fileRetained: true,
      historyRetained: true,
    });
    expect(bodies).toEqual([
      { idempotency_key: "retire-key" },
      { intent_id: "retire-1" },
    ]);
  });
});
