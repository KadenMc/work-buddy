import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CoworkFolderSummary } from "../contracts";
import { sha256Hex } from "../persistence/hashing";
import { CoworkHttpClient } from "../providers/CoworkHttpClient";
import {
  CoworkDocumentLifecycleDialog,
  sameFilePath,
} from "./CoworkDocumentLifecycleDialog";
import {
  COWORK_IMPORTED_TITLE_MAX_CHARS,
  coworkFileConverter,
  coworkImportedTitleFromPath,
} from "./fileImporters";

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

const pickerSourceSha256 = "a".repeat(64);
const replacementPickerSourceSha256 = "b".repeat(64);
const MARKDOWN_IMPORTER_WIRE = {
  importer_id: "markdown/v1",
  display_name: "Markdown",
  source_format: "markdown",
  media_type: "text/markdown",
  suffixes: [".md", ".markdown"],
  max_source_bytes: 16 * 1024 * 1024,
} as const;
const MARKDOWN_PICKER_BINDING = {
  importer_id: MARKDOWN_IMPORTER_WIRE.importer_id,
  media_type: MARKDOWN_IMPORTER_WIRE.media_type,
  importer: MARKDOWN_IMPORTER_WIRE,
} as const;
const ACTOR = {
  kind: "human",
  ref: "dashboard-user",
  identity_status: "local_actor_ref",
} as const;

describe("Co-work file path identity", () => {
  it("is case-insensitive for Windows Folders and case-sensitive for POSIX Folders", () => {
    expect(
      sameFilePath(
        "Notes/Existing.md",
        "notes/existing.md",
        "C:\\Projects\\work-buddy",
      ),
    ).toBe(true);
    expect(
      sameFilePath(
        "Notes/Existing.md",
        "notes/existing.md",
        "/srv/projects/work-buddy",
      ),
    ).toBe(false);
  });
});

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
    const fileName = screen.getByRole("textbox", { name: "File name" });
    await user.clear(fileName);
    await user.type(fileName, unsafePath);
    await user.click(screen.getByRole("button", { name: "Create document" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      /filename without folder separators|safe \.md or \.markdown filename/,
    );
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("uses the native location picker while preserving the active Folder root as default", async () => {
    const user = userEvent.setup();
    const requests: Array<{ readonly url: string; readonly init?: RequestInit }> = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push({ url, init });
      if (url === "/api/truth/cowork/folders/choose-location") {
        return new Response(
          JSON.stringify({ ok: true, cancelled: false, path: "Research/Notes" }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/truth/doc/bootstrap?store_id=store-1") {
        const form = init?.body as FormData;
        const metadata = form.get("metadata");
        expect(typeof metadata).toBe("string");
        expect(JSON.parse(String(metadata))).toMatchObject({
          mode: "create",
          path: "Research/Notes/project-brief.md",
          title: "Project brief",
        });
        return new Response(
          JSON.stringify({
            bootstrap_id: "bootstrap-create",
            document_id: "doc-create",
            mode: "create",
            normalized_path: "Research/Notes/project-brief.md",
            source_sha256:
              "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            source_byte_length: 0,
            source_url: "/bootstrap-create/source",
            ydoc_schema: "yjs-v1",
            expires_at: "2026-07-22T20:00:00Z",
            state: "prepared",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/bootstrap-create/source") return new Response(new Uint8Array(0));
      if (url.startsWith("/api/truth/doc/bootstrap/bootstrap-create?")) {
        return new Response(
          JSON.stringify({
            document_id: "doc-create",
            path: "Research/Notes/project-brief.md",
            title: "Project brief",
            initialization_state: "ready",
            drift_state: "clean",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    render(
      <CoworkDocumentLifecycleDialog
        mode="create"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        initialTitle="Project brief"
        onClose={vi.fn()}
        onOpened={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("group", { name: "Save in" }),
    ).toHaveTextContent("work-buddy");
    expect(screen.getByRole("textbox", { name: "File name" })).toHaveValue(
      "project-brief.md",
    );
    await user.click(screen.getByRole("button", { name: "Change" }));

    await waitFor(() =>
      expect(screen.getByText("work-buddy / Research/Notes")).toBeVisible(),
    );
    const pickerRequest = requests.find(
      (request) => request.url === "/api/truth/cowork/folders/choose-location",
    );
    expect(JSON.parse(String(pickerRequest?.init?.body))).toEqual({
      store_id: "store-1",
    });
    await user.click(screen.getByRole("button", { name: "Create document" }));
    await waitFor(() =>
      expect(
        requests.some((request) =>
          request.url.startsWith("/api/truth/doc/bootstrap/bootstrap-create?"),
        ),
      ).toBe(true),
    );
  }, 20_000);

  it("closes cleanly when the native file picker is cancelled", async () => {
    const onClose = vi.fn();
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      expect(String(input)).toBe("/api/truth/cowork/files/choose-import");
      return new Response(JSON.stringify({ ok: true, cancelled: true }), {
        headers: { "Content-Type": "application/json" },
      });
    });

    render(
      <CoworkDocumentLifecycleDialog
        mode="import"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        provenanceActor={ACTOR}
        onClose={onClose}
        onOpened={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("dialog", { name: "From file" }),
    ).toBeVisible();
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it("determines provenance after selecting a Markdown file before creating a detached Co-work document", async () => {
    const user = userEvent.setup();
    const source = new TextEncoder().encode("# Imported source\n\nKeep this source file unchanged.\n");
    const sourceSha256 = await sha256Hex(source);
    const preparedMetadata: unknown[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/truth/cowork/files/choose-import") {
        return new Response(
          JSON.stringify({
            ok: true,
            cancelled: false,
            path: "drafts/imported-source.md",
            ...MARKDOWN_PICKER_BINDING,
            source_sha256: sourceSha256,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/truth/doc/list?store_id=store-1") {
        return new Response(JSON.stringify({ docs: [] }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/truth/doc/bootstrap?store_id=store-1") {
        const form = init?.body as FormData;
        const metadata = form.get("metadata");
        expect(typeof metadata).toBe("string");
        preparedMetadata.push(JSON.parse(String(metadata)));
        expect(form.get("source")).toBeNull();
        return new Response(
          JSON.stringify({
            bootstrap_id: "bootstrap-import",
            document_id: "doc-import",
            mode: "import",
            normalized_path: "drafts/imported-source.md",
            source_sha256: sourceSha256,
            source_byte_length: source.byteLength,
            source_url: "/bootstrap-import/source",
            ydoc_schema: "yjs-v1",
            expires_at: "2026-07-22T20:00:00Z",
            state: "prepared",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/bootstrap-import/source") {
        return new Response(source, { status: 200 });
      }
      if (url.startsWith("/api/truth/doc/bootstrap/bootstrap-import?")) {
        const form = init?.body as FormData;
        expect(init?.method).toBe("PUT");
        expect(form.get("snapshot")).toBeInstanceOf(Blob);
        expect(form.get("projection")).toBeInstanceOf(Blob);
        expect(JSON.parse(String(form.get("metadata")))).toMatchObject({
          source_sha256: sourceSha256,
          ydoc_schema: "yjs-v1",
        });
        return new Response(
          JSON.stringify({
            document_id: "doc-import",
            path: "drafts/imported-source.md",
            title: "Imported source",
            initialization_state: "ready",
            drift_state: "clean",
            source_writeback: "never",
            permissions: { open: true, edit: true, materialize: false, repair: true, retire: true },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const onOpened = vi.fn(async () => undefined);

    render(
      <CoworkDocumentLifecycleDialog
        mode="import"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        provenanceActor={ACTOR}
        onClose={vi.fn()}
        onOpened={onOpened}
      />,
    );

    expect(await screen.findByText("imported-source.md")).toBeVisible();
    expect(screen.getByTitle("drafts/imported-source.md")).toHaveTextContent(
      "Markdown",
    );
    expect(
      screen.getByRole("heading", { name: "Where did this text come from?" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: /Authorship/i })).toHaveTextContent(
      "Unknown",
    );
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Import" }));

    await waitFor(() => expect(onOpened).toHaveBeenCalledOnce());
    expect(preparedMetadata).toEqual([
      expect.objectContaining({
        mode: "import",
        path: "drafts/imported-source.md",
        importer_id: "markdown/v1",
        source_media_type: "text/markdown",
        expected_file_sha256: sourceSha256,
        authorship_attestation: {
          schema: "cowork-authorship-attestation/v1",
          authorship: {
            kind: "unknown",
            contributors: [],
          },
          human_review: { status: "not_applicable", reviewers: [] },
        },
      }),
    ]);
    expect(onOpened).toHaveBeenCalledWith(
      expect.objectContaining({
        documentId: "doc-import",
        sourceWriteback: "never",
        permissions: expect.objectContaining({ materialize: false }),
      }),
    );
  }, 20_000);

  it("resets provenance for each valid replacement and preserves the prior selection when re-choosing is cancelled or invalid", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const pickerResults = [
      {
        cancelled: false,
        path: "drafts/first.md",
        ...MARKDOWN_PICKER_BINDING,
        source_sha256: pickerSourceSha256,
      },
      {
        cancelled: false,
        path: "drafts/second.md",
        ...MARKDOWN_PICKER_BINDING,
        source_sha256: replacementPickerSourceSha256,
      },
      { cancelled: true },
      {
        cancelled: false,
        path: "drafts/unsupported.md",
        importer_id: "word/v1",
        media_type:
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_sha256: "c".repeat(64),
        importer: {
          importer_id: "word/v1",
          display_name: "Word document",
          source_format: "word",
          media_type:
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          suffixes: [".docx"],
          max_source_bytes: 16 * 1024 * 1024,
        },
      },
    ];
    let pickerCall = 0;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/truth/cowork/files/choose-import") {
        return new Response(JSON.stringify(pickerResults[pickerCall++]), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/truth/doc/list?store_id=store-1") {
        return new Response(JSON.stringify({ docs: [] }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <CoworkDocumentLifecycleDialog
        mode="import"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        provenanceActor={ACTOR}
        onClose={onClose}
        onOpened={vi.fn()}
      />,
    );

    expect(await screen.findByText("first.md", { exact: true })).toBeVisible();
    await user.click(screen.getByRole("button", { name: /Authorship/i }));
    await user.click(screen.getByRole("option", { name: /^Human-written/ }));
    expect(screen.getByRole("button", { name: /Authorship/i })).toHaveTextContent(
      "Human-written",
    );

    await user.click(screen.getByRole("button", { name: "Choose another file" }));
    expect(await screen.findByText("second.md", { exact: true })).toBeVisible();
    expect(screen.getByRole("button", { name: /Authorship/i })).toHaveTextContent(
      "Unknown",
    );

    await user.click(screen.getByRole("button", { name: "Choose another file" }));
    await waitFor(() => expect(pickerCall).toBe(3));
    expect(screen.getByRole("dialog", { name: "From file" })).toBeVisible();
    expect(screen.getByText("second.md", { exact: true })).toBeVisible();
    expect(onClose).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Choose another file" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "doesn’t include the converter version selected for that file",
    );
    expect(screen.getByText("second.md", { exact: true })).toBeVisible();
    expect(screen.queryByText("unsupported.md", { exact: true })).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("opens a Windows file path that is already registered with different casing", async () => {
    const onClose = vi.fn();
    const onOpened = vi.fn(async () => undefined);
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/truth/cowork/files/choose-import") {
        return new Response(
          JSON.stringify({
            ok: true,
            cancelled: false,
            path: "Notes/EXISTING.md",
            ...MARKDOWN_PICKER_BINDING,
            source_sha256: pickerSourceSha256,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/truth/doc/list?store_id=store-1") {
        return new Response(
          JSON.stringify({
            docs: [
              {
                document_id: "doc-existing",
                path: "notes/existing.md",
                title: "Existing",
                initialization_state: "ready",
                drift_state: "clean",
                source_writeback: "never",
                import_source_sha256: pickerSourceSha256,
              },
            ],
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <CoworkDocumentLifecycleDialog
        mode="import"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        provenanceActor={ACTOR}
        onClose={onClose}
        onOpened={onOpened}
      />,
    );

    await waitFor(() => expect(onOpened).toHaveBeenCalledOnce());
    expect(onOpened).toHaveBeenCalledWith(
      expect.objectContaining({ documentId: "doc-existing" }),
    );
    expect(onClose).toHaveBeenCalledOnce();
    expect(
      fetchImpl.mock.calls.some(([input]) =>
        String(input).includes("/api/truth/doc/bootstrap"),
      ),
    ).toBe(false);
  });

  it.each([
    {
      label: "changed",
      recordedSourceSha256: pickerSourceSha256,
      selectedSourceSha256: replacementPickerSourceSha256,
      warning: "This file has changed since it was imported.",
    },
    {
      label: "unconfirmed",
      recordedSourceSha256: null,
      selectedSourceSha256: replacementPickerSourceSha256,
      warning: "Co-work can’t confirm which version of this file was imported.",
    },
  ])(
    "requires an explicit choice before opening an existing detached copy when source identity is $label",
    async ({ recordedSourceSha256, selectedSourceSha256, warning }) => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      const onOpened = vi.fn(async () => undefined);
      const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/truth/cowork/files/choose-import") {
          return new Response(
            JSON.stringify({
              ok: true,
              cancelled: false,
              path: "notes/existing.md",
              ...MARKDOWN_PICKER_BINDING,
              source_sha256: selectedSourceSha256,
            }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        if (url === "/api/truth/doc/list?store_id=store-1") {
          return new Response(
            JSON.stringify({
              docs: [
                {
                  document_id: "doc-existing",
                  path: "notes/existing.md",
                  title: "Existing",
                  initialization_state: "ready",
                  drift_state: "clean",
                  source_writeback: "never",
                  import_source_sha256: recordedSourceSha256,
                  observed_source_file_sha256: selectedSourceSha256,
                },
              ],
            }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      });

      render(
        <CoworkDocumentLifecycleDialog
          mode="import"
          folder={folder}
          client={new CoworkHttpClient(fetchImpl as typeof fetch)}
          provenanceActor={ACTOR}
          onClose={onClose}
          onOpened={onOpened}
        />,
      );

      expect(await screen.findByRole("alert")).toHaveTextContent(warning);
      expect(screen.getByRole("button", { name: "Choose another file" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
      expect(onOpened).not.toHaveBeenCalled();
      expect(screen.queryByRole("button", { name: "Import" })).toBeNull();

      await user.click(
        screen.getByRole("button", { name: "Open existing Co-work copy" }),
      );
      await waitFor(() => expect(onOpened).toHaveBeenCalledOnce());
      expect(onOpened).toHaveBeenCalledWith(
        expect.objectContaining({ documentId: "doc-existing" }),
      );
      expect(onClose).toHaveBeenCalledOnce();
      expect(
        fetchImpl.mock.calls.some(([input]) =>
          String(input).includes("/api/truth/doc/bootstrap"),
        ),
      ).toBe(false);
    },
  );

  it("recovers the authoritative document when bootstrap reports an identity conflict", async () => {
    const onClose = vi.fn();
    const onOpened = vi.fn(async () => undefined);
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/truth/cowork/files/choose-import") {
        return new Response(
          JSON.stringify({
            ok: true,
            cancelled: false,
            path: "notes/existing.md",
            ...MARKDOWN_PICKER_BINDING,
            source_sha256: pickerSourceSha256,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/truth/doc/list?store_id=store-1") {
        return new Response(JSON.stringify({ docs: [] }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/truth/doc/bootstrap?store_id=store-1") {
        return new Response(
          JSON.stringify({
            error: {
              code: "already_registered",
              message: "Markdown path is already registered",
              retryable: false,
              details: { document_id: "doc-existing" },
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/truth/doc/doc-existing?store_id=store-1") {
        return new Response(
          JSON.stringify({
            document_id: "doc-existing",
            path: "notes/existing.md",
            title: "Existing",
            initialization_state: "ready",
            drift_state: "clean",
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <CoworkDocumentLifecycleDialog
        mode="import"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        provenanceActor={ACTOR}
        onClose={onClose}
        onOpened={onOpened}
      />,
    );

    await screen.findByRole("heading", { name: "Where did this text come from?" });
    await userEvent.setup().click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(onOpened).toHaveBeenCalledOnce());
    expect(onOpened).toHaveBeenCalledWith(
      expect.objectContaining({ documentId: "doc-existing" }),
    );
    expect(onClose).toHaveBeenCalledOnce();
  });

  it.each([
    ["unchanged", pickerSourceSha256],
    ["changed", replacementPickerSourceSha256],
  ])(
    "shows retired-state guidance without an impossible open action when the selected source is %s",
    async (_sourceState, selectedSourceSha256) => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      const onOpened = vi.fn(async () => undefined);
      const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/truth/cowork/files/choose-import") {
          return new Response(
            JSON.stringify({
              ok: true,
              cancelled: false,
              path: "notes/retired.md",
              ...MARKDOWN_PICKER_BINDING,
              source_sha256: selectedSourceSha256,
            }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        if (url === "/api/truth/doc/list?store_id=store-1") {
          return new Response(JSON.stringify({ docs: [] }), {
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url === "/api/truth/doc/bootstrap?store_id=store-1") {
          return new Response(
            JSON.stringify({
              error: {
                code: "retired_path",
                message:
                  "This file already has a retired Co-work copy. Its history is preserved, so this path cannot be reused.",
                retryable: false,
                details: {
                  document_id: "doc-retired",
                  lifecycle: "retired",
                  path_reuse: "forbidden",
                  recovery_action: "choose_different_path",
                },
              },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      });

      render(
        <CoworkDocumentLifecycleDialog
          mode="import"
          folder={folder}
          client={new CoworkHttpClient(fetchImpl as typeof fetch)}
          provenanceActor={ACTOR}
          onClose={onClose}
          onOpened={onOpened}
        />,
      );

      await screen.findByRole("heading", { name: "Where did this text come from?" });
      await user.click(screen.getByRole("button", { name: "Import" }));

      const guidance = await screen.findByRole("alert");
      expect(guidance).toHaveTextContent(
        "A Co-work copy of this file was retired.",
      );
      expect(guidance).toHaveTextContent(
        "copy or rename this file to import it as a new document",
      );
      expect(
        screen.queryByRole("button", { name: "Open existing Co-work copy" }),
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Import" })).toBeNull();
      expect(
        screen.getByRole("button", { name: "Choose another file" }),
      ).toBeEnabled();
      expect(onOpened).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
      expect(
        fetchImpl.mock.calls.some(([input]) =>
          String(input).includes("/api/truth/doc/doc-retired"),
        ),
      ).toBe(false);
    },
  );

  it.each([
    ["unchanged", pickerSourceSha256],
    ["changed", replacementPickerSourceSha256],
  ])(
    "treats an already-registered document that retires before recovery as terminal when the source is %s",
    async (_sourceState, selectedSourceSha256) => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      const onOpened = vi.fn(async () => undefined);
      const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/truth/cowork/files/choose-import") {
          return new Response(
            JSON.stringify({
              ok: true,
              cancelled: false,
              path: "notes/retired-during-recovery.md",
              ...MARKDOWN_PICKER_BINDING,
              source_sha256: selectedSourceSha256,
            }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        if (url === "/api/truth/doc/list?store_id=store-1") {
          return new Response(JSON.stringify({ docs: [] }), {
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url === "/api/truth/doc/bootstrap?store_id=store-1") {
          return new Response(
            JSON.stringify({
              error: {
                code: "already_registered",
                message: "File path is already registered",
                retryable: false,
                details: { document_id: "doc-retired-during-recovery" },
              },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
        if (
          url ===
          "/api/truth/doc/doc-retired-during-recovery?store_id=store-1"
        ) {
          return new Response(
            JSON.stringify({
              document_id: "doc-retired-during-recovery",
              path: "notes/retired-during-recovery.md",
              title: "Retired during recovery",
              initialization_state: "ready",
              lifecycle: "retired",
              drift_state: "clean",
              source_writeback: "never",
              import_source_sha256: pickerSourceSha256,
              observed_source_file_sha256: selectedSourceSha256,
            }),
            { headers: { "Content-Type": "application/json" } },
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      });

      render(
        <CoworkDocumentLifecycleDialog
          mode="import"
          folder={folder}
          client={new CoworkHttpClient(fetchImpl as typeof fetch)}
          provenanceActor={ACTOR}
          onClose={onClose}
          onOpened={onOpened}
        />,
      );

      await screen.findByRole("heading", {
        name: "Where did this text come from?",
      });
      await user.click(screen.getByRole("button", { name: "Import" }));

      const guidance = await screen.findByRole("alert");
      expect(guidance).toHaveTextContent(
        "A Co-work copy of this file was retired.",
      );
      expect(
        screen.queryByRole("button", { name: "Open existing Co-work copy" }),
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Import" })).toBeNull();
      expect(
        screen.getByRole("button", { name: "Choose another file" }),
      ).toBeEnabled();
      expect(onOpened).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
    },
  );

  it("does not silently open a changed detached copy discovered during bootstrap", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onOpened = vi.fn(async () => undefined);
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/truth/cowork/files/choose-import") {
        return new Response(
          JSON.stringify({
            ok: true,
            cancelled: false,
            path: "notes/existing.md",
            ...MARKDOWN_PICKER_BINDING,
            source_sha256: replacementPickerSourceSha256,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/truth/doc/list?store_id=store-1") {
        return new Response(JSON.stringify({ docs: [] }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/truth/doc/bootstrap?store_id=store-1") {
        return new Response(
          JSON.stringify({
            error: {
              code: "already_registered",
              message: "Markdown path is already registered",
              retryable: false,
              details: { document_id: "doc-existing" },
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/truth/doc/doc-existing?store_id=store-1") {
        return new Response(
          JSON.stringify({
            document_id: "doc-existing",
            path: "notes/existing.md",
            title: "Existing",
            initialization_state: "ready",
            drift_state: "clean",
            source_writeback: "never",
            import_source_sha256: pickerSourceSha256,
            observed_source_file_sha256: replacementPickerSourceSha256,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <CoworkDocumentLifecycleDialog
        mode="import"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        provenanceActor={ACTOR}
        onClose={onClose}
        onOpened={onOpened}
      />,
    );

    await screen.findByRole("heading", { name: "Where did this text come from?" });
    await user.click(screen.getByRole("button", { name: "Import" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This file has changed since it was imported.",
    );
    expect(onOpened).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", { name: "Open existing Co-work copy" }),
    );
    await waitFor(() => expect(onOpened).toHaveBeenCalledOnce());
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("offers Choose again with contextual copy after a recoverable picker failure", async () => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({
          error: {
            code: "folder_chooser_failed",
            message: "internal picker detail",
            retryable: true,
          },
        }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(
      <CoworkDocumentLifecycleDialog
        mode="import"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        provenanceActor={ACTOR}
        onClose={vi.fn()}
        onOpened={vi.fn()}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The file picker couldn’t be opened.",
    );
    await user.click(screen.getByRole("button", { name: "Choose again" }));
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
  });

  it("does not call the file picker when file import is unavailable", () => {
    const fetchImpl = vi.fn(async () =>
      new Response(null, { status: 500 }),
    );
    render(
      <CoworkDocumentLifecycleDialog
        mode="import"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        provenanceActor={ACTOR}
        filePickerAvailable={false}
        onClose={vi.fn()}
        onOpened={vi.fn()}
      />,
    );

    expect(
      screen.getByText("File import isn’t available here."),
    ).toBeVisible();
    expect(
      screen.queryByText("Opening file picker…"),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Choose again" }),
    ).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("keeps root creation available while explaining an unavailable destination picker", async () => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn(async () =>
      new Response(null, { status: 500 }),
    );
    render(
      <CoworkDocumentLifecycleDialog
        mode="create"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        locationPickerAvailable={false}
        onClose={vi.fn()}
        onOpened={vi.fn()}
      />,
    );

    const change = screen.getByRole("button", { name: "Change" });
    expect(change).toBeDisabled();
    expect(change).toHaveAccessibleDescription(
      "Choosing another save location isn’t available here. You can still save in work-buddy.",
    );
    expect(
      screen.getByText(
        "Choosing another save location isn’t available here. You can still save in work-buddy.",
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Create document" }),
    ).toBeEnabled();

    await user.click(change);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("offers a direct identity retry without making the user choose the file again", async () => {
    const user = userEvent.setup();
    let actorAttempts = 0;
    let pickerAttempts = 0;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/truth/cowork/files/choose-import") {
        pickerAttempts += 1;
        return new Response(
          JSON.stringify({
            cancelled: false,
            path: "drafts/retry-identity.md",
            ...MARKDOWN_PICKER_BINDING,
            source_sha256: pickerSourceSha256,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/truth/doc/list?store_id=store-1") {
        return new Response(JSON.stringify({ docs: [] }), {
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/truth/cowork/current-actor") {
        actorAttempts += 1;
        if (actorAttempts === 1) {
          return new Response(
            JSON.stringify({
              error: {
                code: "identity_unavailable",
                message: "Identity service is unavailable.",
                retryable: true,
              },
            }),
            {
              status: 503,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        return new Response(
          JSON.stringify({
            kind: "human",
            ref: ACTOR.ref,
            identity_status: ACTOR.identity_status,
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <CoworkDocumentLifecycleDialog
        mode="import"
        folder={folder}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        onClose={vi.fn()}
        onOpened={vi.fn()}
      />,
    );

    expect(
      await screen.findByText("Identity service is unavailable."),
    ).toBeVisible();
    expect(
      screen.queryByText("Checking the current identity…"),
    ).toBeNull();
    expect(screen.getByText("retry-identity.md", { exact: true })).toBeVisible();
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();

    await user.click(
      screen.getByRole("button", { name: "Retry identity" }),
    );
    expect(
      await screen.findByRole("button", { name: /Authorship/i }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
    expect(actorAttempts).toBe(2);
    expect(pickerAttempts).toBe(1);
  });

  it("looks up converters only by versioned ID and derives bounded titles from server suffixes", () => {
    expect(coworkFileConverter("markdown/v1")).not.toBeNull();
    expect(coworkFileConverter("markdown/v2")).toBeNull();
    expect(
      coworkImportedTitleFromPath(
        "Docs/My Working Note.MD",
        MARKDOWN_IMPORTER_WIRE.suffixes,
      ),
    ).toBe(
      "My Working Note",
    );
    const longTitle = "😀".repeat(COWORK_IMPORTED_TITLE_MAX_CHARS + 20);
    const capped = coworkImportedTitleFromPath(
      `Docs/${longTitle}.md`,
      MARKDOWN_IMPORTER_WIRE.suffixes,
    );
    expect(Array.from(capped)).toHaveLength(COWORK_IMPORTED_TITLE_MAX_CHARS);
    expect(capped.endsWith("…")).toBe(true);
  });
});
