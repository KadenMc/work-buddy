import { Editor } from "@tiptap/core";
import * as Y from "yjs";

import type {
  CoworkDocumentPermissions,
  CoworkDocumentSummary,
  CoworkFolderCandidate,
  CoworkFolderChooserAvailability,
  CoworkFolderPermissions,
  CoworkFolderSummary,
} from "../contracts";
import { buildEditorExtensions } from "../editor/extensions";
import { HttpCoworkYdocTransport } from "../persistence/HttpCoworkYdocTransport";
import type {
  CoworkPasteProvenanceRequest,
  CoworkProvenanceActorIdentity,
  CoworkProvenanceDetermination,
} from "../provenance";
import { CoworkHttpError, normalizeCoworkError } from "./errors";

type JsonRecord = Record<string, unknown>;

export const COWORK_FOLDER_PICKER_INTENT_HEADER = "X-Work-Buddy-Intent";
export const COWORK_FOLDER_PICKER_INTENT = "cowork-folder-picker";
/** @deprecated Used only by chooseMarkdownFile compatibility calls. */
export const COWORK_MARKDOWN_PICKER_INTENT = "cowork-markdown-picker";
export const COWORK_IMPORT_PICKER_INTENT = "cowork-import-picker";
export const COWORK_LOCATION_PICKER_INTENT = "cowork-location-picker";

const record = (value: unknown): JsonRecord =>
  typeof value === "object" && value !== null ? (value as JsonRecord) : {};
const text = (value: unknown, fallback = ""): string =>
  typeof value === "string" ? value : fallback;
const bool = (value: unknown, fallback = false): boolean =>
  typeof value === "boolean" ? value : fallback;
const count = (value: unknown, fallback = 0): number =>
  typeof value === "number" && Number.isFinite(value) ? value : fallback;
const nullableText = (value: unknown): string | null =>
  typeof value === "string" ? value : null;

const normalizeNativePathResult = (
  payload: JsonRecord,
  label: string,
): CoworkNativePathResult => {
  const cancelled = bool(payload.cancelled);
  if (cancelled) return { cancelled: true, path: "" };
  if (typeof payload.path !== "string") {
    throw new CoworkHttpError({
      code: "invalid_picker_response",
      message: `The ${label} picker returned an invalid selection.`,
      retryable: true,
    });
  }
  return { cancelled: false, path: payload.path };
};

const IMPORTER_ID = /^[a-z][a-z0-9._-]{0,63}\/v[1-9][0-9]{0,8}$/u;
const SOURCE_FORMAT = /^[a-z][a-z0-9._-]{0,39}$/u;
const MEDIA_TYPE =
  /^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}\/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$/u;
const IMPORT_SUFFIX = /^\.[A-Za-z0-9][A-Za-z0-9._+-]{0,15}$/u;

const normalizeImportDescriptor = (
  value: unknown,
): CoworkImportDescriptor | null => {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  const source = value as JsonRecord;
  const importerId = text(source.importer_id);
  const displayName = text(source.display_name);
  const sourceFormat = text(source.source_format);
  const mediaType = text(source.media_type);
  const rawSuffixes = source.suffixes;
  const maxSourceBytes = source.max_source_bytes;
  if (
    !IMPORTER_ID.test(importerId) ||
    displayName.length === 0 ||
    displayName !== displayName.trim() ||
    Array.from(displayName).length > 80 ||
    /[\u0000-\u001f]/u.test(displayName) ||
    !SOURCE_FORMAT.test(sourceFormat) ||
    !MEDIA_TYPE.test(mediaType) ||
    !Array.isArray(rawSuffixes) ||
    rawSuffixes.length === 0 ||
    rawSuffixes.some(
      (suffix) => typeof suffix !== "string" || !IMPORT_SUFFIX.test(suffix),
    ) ||
    new Set(rawSuffixes.map((suffix) => String(suffix).toLocaleLowerCase("en-US")))
      .size !== rawSuffixes.length ||
    typeof maxSourceBytes !== "number" ||
    !Number.isSafeInteger(maxSourceBytes) ||
    maxSourceBytes <= 0
  ) {
    return null;
  }
  return Object.freeze({
    importerId,
    displayName,
    sourceFormat,
    mediaType,
    suffixes: Object.freeze(rawSuffixes.map((suffix) => String(suffix))),
    maxSourceBytes,
  });
};

export interface CoworkFolderListResult {
  readonly readOnly: boolean;
  readonly folders: readonly CoworkFolderSummary[];
  readonly diagnostics: readonly unknown[];
  readonly chooser: CoworkFolderChooserAvailability;
}

export interface CoworkChooseResult extends CoworkFolderCandidate {
  readonly cancelled: boolean;
  readonly selectionToken: string | null;
}

export interface CoworkNativePathResult {
  readonly cancelled: boolean;
  /** Folder-relative path. An empty path is the active Folder root. */
  readonly path: string;
}

export interface CoworkImportDescriptor {
  /** Exact versioned converter contract selected by the server. */
  readonly importerId: string;
  readonly displayName: string;
  readonly sourceFormat: string;
  readonly mediaType: string;
  readonly suffixes: readonly string[];
  readonly maxSourceBytes: number;
}

interface CoworkSelectedImportPathResult extends CoworkNativePathResult {
  readonly cancelled: false;
  /** Format/version-neutral converter identity selected by the server registry. */
  readonly importerId: string;
  /** Source media type supplied to that converter; not an editor projection type. */
  readonly mediaType: string;
  /** Hash of the unchanged selected source file. */
  readonly sourceSha256: string;
  /** Server-authoritative source-format and admission descriptor. */
  readonly importer: CoworkImportDescriptor;
}

export type CoworkImportPathResult =
  | CoworkSelectedImportPathResult
  | {
      readonly cancelled: true;
      readonly path: "";
      readonly importerId: "";
      readonly mediaType: "";
      readonly sourceSha256: "";
      readonly importer: null;
    };

export type CoworkInspectionStatus =
  | "inspection_pending"
  | "initialized"
  | "uninitialized"
  | "inside_existing_folder"
  | "contains_nested_folder"
  | "collision"
  | "unavailable";

export interface CoworkInspectionResult {
  readonly status: CoworkInspectionStatus;
  readonly candidate: CoworkFolderCandidate | null;
  readonly folder: CoworkFolderSummary | null;
  readonly owner: CoworkFolderSummary | null;
  readonly boundaries: readonly (CoworkFolderCandidate & { readonly storeId: string | null })[];
  readonly reasonCode: string | null;
  readonly actions: readonly string[];
  readonly inspectionToken: string | null;
  readonly continuationToken: string | null;
  readonly progress: { readonly visited: number; readonly complete: false } | null;
  readonly retryAfterMs: number | null;
}

export interface CoworkCandidateDocument {
  readonly path: string;
  readonly title: string;
  readonly byteSize: number;
  readonly modifiedAt: string | null;
  readonly alreadyRegistered: boolean;
}

export interface CoworkCandidatesResult {
  readonly candidates: readonly CoworkCandidateDocument[];
  readonly cursor: string | null;
}

export interface CoworkBootstrapPrepared {
  readonly bootstrapId: string;
  readonly documentId: string;
  readonly mode: "create" | "import" | "repair";
  readonly normalizedPath: string;
  readonly sourceSha256: string;
  readonly sourceByteLength: number;
  readonly sourceUrl: string;
  readonly ydocSchema: string;
  readonly expiresAt: string;
  readonly state: "prepared" | "publishing" | "committed" | "cancelled" | "failed";
  readonly result: CoworkDocumentSummary | null;
}

export interface CoworkBootstrapMetadata {
  readonly mode: "create" | "import" | "repair";
  readonly path: string;
  readonly title?: string;
  readonly initialSourceSha256?: string;
  readonly importerId?: string;
  readonly sourceMediaType?: string;
  readonly authorshipAttestation?: CoworkProvenanceDetermination;
  readonly expectedFileSha256?: string | null;
  readonly documentId?: string | null;
  readonly idempotencyKey: string;
}

export interface CoworkPasteProvenanceReceipt {
  readonly attestationId: string;
  readonly documentSpanId: string;
  readonly targetStructuredHeadSha256: string;
}

export interface CoworkDriftSource {
  readonly available: boolean;
  readonly sha256: string | null;
  readonly etag: string | null;
  readonly sourceUrl: string;
}

export interface CoworkDriftInspection {
  readonly state: "clean" | "drifted" | "missing";
  readonly lastMaterializedSha256: string;
  readonly currentFileSha256: string | null;
  readonly snapshotSha256: string | null;
  readonly structuredHeadSha256: string | null;
  readonly updateTailPresent: boolean;
  readonly unmaterializedStructuredEdits: boolean;
  readonly baseline: CoworkDriftSource;
  readonly source: CoworkDriftSource;
  readonly diffAvailable: boolean;
  readonly canReimport: boolean;
}

export interface CoworkReimportReceipt {
  readonly intentId: string;
  readonly documentId: string;
  readonly sourceSha256: string;
  readonly snapshotSha256: string;
  readonly structuredHeadSha256: string;
  readonly documentVersionId: string;
  readonly docEventId: string;
  readonly staledProposalIds: readonly string[];
  readonly reimportedAt: string;
}

export interface CoworkReimportPrepared {
  readonly intentId: string;
  readonly state: "prepared" | "committed" | "cancelled";
  readonly expiresAt: string;
  readonly sourceSha256: string;
  readonly sourceByteLength: number;
  readonly priorProjectionSha256: string;
  readonly priorSnapshotSha256: string;
  readonly priorStructuredHeadSha256: string;
  readonly consequence: string;
  readonly result: CoworkReimportReceipt | null;
}

export interface CoworkRetirementReceipt {
  readonly intentId: string;
  readonly documentId: string;
  readonly lifecycle: "retired";
  readonly retiredAt: string;
  readonly docEventId: string;
  readonly fileRetained: boolean;
  readonly historyRetained: boolean;
}

export interface CoworkRetirementPrepared {
  readonly intentId: string;
  readonly expiresAt: string;
  readonly documentId: string;
  readonly consequence: string;
  readonly consequenceSha256: string;
}

const normalizeFolderPermissions = (value: unknown): CoworkFolderPermissions => {
  const source = record(value);
  return {
    read: bool(source.read, true),
    create: bool(source.create),
    import: bool(source.import),
    materialize: bool(source.materialize),
    retire: bool(source.retire),
  };
};

export const normalizeFolderSummary = (value: unknown): CoworkFolderSummary => {
  const source = record(value);
  const documentSurface = record(source.document_surface ?? source.documentSurface);
  const classes = documentSurface.allowed_document_classes ?? documentSurface.allowedDocumentClasses;
  return {
    storeId: text(source.store_id ?? source.storeId),
    folderName: text(source.folder_name ?? source.folderName),
    folderPath: text(source.folder_path ?? source.folderPath),
    layout: text(source.layout, "wbuddy_cowork_v1"),
    reachable: bool(source.reachable, true),
    eligibility: text(source.eligibility, "eligible"),
    ineligibleReason: nullableText(source.ineligible_reason ?? source.ineligibleReason),
    documentSurface: {
      enabled: bool(documentSurface.enabled, true),
      allowedDocumentClasses: Array.isArray(classes)
        ? classes.filter((entry): entry is string => typeof entry === "string")
        : ["co_authored"],
      feedbackCapture: bool(
        documentSurface.feedback_capture ?? documentSurface.feedbackCapture,
      ),
    },
    permissions: normalizeFolderPermissions(source.permissions),
    documentCount: count(source.document_count ?? source.documentCount),
  };
};

const normalizeDocumentPermissions = (value: unknown): CoworkDocumentPermissions => {
  const source = record(value);
  return {
    open: bool(source.open, true),
    edit: bool(source.edit),
    materialize: bool(source.materialize),
    repair: bool(source.repair),
    retire: bool(source.retire),
  };
};

export const normalizeDocumentSummary = (value: unknown): CoworkDocumentSummary => {
  const source = record(value);
  const hashes = record(source.hashes);
  const readiness = record(source.readiness);
  const drift = record(source.drift);
  const initialization = text(
    source.initialization_state ??
      source.initializationState ??
      readiness.initialization_state,
    "ready",
  ) as CoworkDocumentSummary["initializationState"];
  const rawDrift = text(
    source.drift_state ?? source.driftState ?? drift.state,
    "clean",
  );
  const path = text(source.path);
  const suppliedTitle = text(source.title).trim();
  const pathParts = path.split(/[\\/]/u);
  const fallbackTitle = pathParts[pathParts.length - 1] || path || "Untitled document";
  const hasSnakeCaseWriteback = Object.prototype.hasOwnProperty.call(
    source,
    "source_writeback",
  );
  const hasCamelCaseWriteback = Object.prototype.hasOwnProperty.call(
    source,
    "sourceWriteback",
  );
  const rawSourceWriteback = hasSnakeCaseWriteback
    ? source.source_writeback
    : hasCamelCaseWriteback
      ? source.sourceWriteback
      : undefined;
  return {
    documentId: text(source.document_id ?? source.documentId),
    path,
    title: suppliedTitle.length > 0 ? suppliedTitle : fallbackTitle,
    profile: text(source.profile ?? source.document_class, "co_authored"),
    documentClass: text(source.document_class ?? source.documentClass, "co_authored"),
    // Absence identifies a legacy payload from before this field existed.
    // Once a server supplies the field, accept only the exact known values
    // and fail closed for malformed or future values.
    sourceWriteback:
      !hasSnakeCaseWriteback && !hasCamelCaseWriteback
        ? "same_file"
        : rawSourceWriteback === "same_file"
          ? "same_file"
          : "never",
    lifecycle: text(source.lifecycle, "active") as "active" | "retired",
    initializationState: initialization,
    structuredHeadSha256: nullableText(
      source.structured_head_sha256 ??
        source.structuredHeadSha256 ??
        readiness.structured_head_sha256,
    ),
    snapshotSha256: nullableText(
      source.snapshot_sha256 ??
        source.snapshotSha256 ??
        readiness.snapshot_sha256 ??
        hashes.ydoc_snapshot_sha256,
    ),
    projectionSha256: nullableText(
      source.projection_sha256 ??
        source.projectionSha256 ??
        readiness.projection_sha256 ??
        hashes.last_materialized_sha256,
    ),
    currentFileSha256: nullableText(
      source.current_file_sha256 ??
        source.currentFileSha256 ??
        hashes.current_file_sha256,
    ),
    importSourceSha256: nullableText(
      source.import_source_sha256 ??
        source.importSourceSha256 ??
        hashes.import_source_sha256 ??
        hashes.importSourceSha256,
    ),
    observedSourceFileSha256: nullableText(
      source.observed_source_file_sha256 ??
        source.observedSourceFileSha256 ??
        source.source_file_sha256 ??
        source.sourceFileSha256 ??
        hashes.observed_source_file_sha256 ??
        hashes.observedSourceFileSha256 ??
        hashes.source_file_sha256,
    ),
    projectionBlobAvailable: bool(
      source.projection_blob_available ?? source.projectionBlobAvailable,
    ),
    driftState:
      rawDrift === "drifted" || rawDrift === "missing" ? rawDrift : "clean",
    openProposalCount: count(source.open_proposal_count ?? source.openProposalCount),
    openFlagCount: count(source.open_flag_count ?? source.openFlagCount),
    updatedAt: nullableText(source.updated_at ?? source.updatedAt),
    permissions: normalizeDocumentPermissions(source.permissions ?? readiness.permissions),
    disabledReason: nullableText(
      source.disabled_reason ?? source.disabledReason ?? readiness.disabled_reason,
    ),
  };
};

const normalizeDriftSource = (value: unknown): CoworkDriftSource => {
  const source = record(value);
  return {
    available: bool(source.available),
    sha256: nullableText(source.sha256),
    etag: nullableText(source.etag),
    sourceUrl: text(source.source_url ?? source.sourceUrl),
  };
};

const normalizeReimportReceipt = (value: unknown): CoworkReimportReceipt => {
  const source = record(value);
  const stale = source.staled_proposal_ids ?? source.staledProposalIds;
  return {
    intentId: text(source.intent_id ?? source.intentId),
    documentId: text(source.document_id ?? source.documentId),
    sourceSha256: text(source.source_sha256 ?? source.sourceSha256),
    snapshotSha256: text(source.snapshot_sha256 ?? source.snapshotSha256),
    structuredHeadSha256: text(
      source.structured_head_sha256 ?? source.structuredHeadSha256,
    ),
    documentVersionId: text(
      source.document_version_id ?? source.documentVersionId,
    ),
    docEventId: text(source.doc_event_id ?? source.docEventId),
    staledProposalIds: Array.isArray(stale)
      ? stale.filter((entry): entry is string => typeof entry === "string")
      : [],
    reimportedAt: text(source.reimported_at ?? source.reimportedAt),
  };
};

const normalizeRetirementReceipt = (value: unknown): CoworkRetirementReceipt => {
  const source = record(value);
  return {
    intentId: text(source.intent_id ?? source.intentId),
    documentId: text(source.document_id ?? source.documentId),
    lifecycle: "retired",
    retiredAt: text(source.retired_at ?? source.retiredAt),
    docEventId: text(source.doc_event_id ?? source.docEventId),
    fileRetained: bool(source.file_retained ?? source.fileRetained),
    historyRetained: bool(source.history_retained ?? source.historyRetained),
  };
};

export class CoworkHttpClient {
  readonly #fetch: typeof fetch;

  constructor(fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.#fetch = fetchImpl;
  }

  async #json(
    url: string,
    init: RequestInit = {},
  ): Promise<JsonRecord> {
    let response: Response;
    try {
      response = await this.#fetch(url, { credentials: "same-origin", ...init });
    } catch (error) {
      throw new CoworkHttpError(normalizeCoworkError(error));
    }
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (!response.ok) {
      throw new CoworkHttpError(normalizeCoworkError(payload, response.status));
    }
    return record(payload);
  }

  async currentActor(): Promise<CoworkProvenanceActorIdentity> {
    const payload = await this.#json("/api/truth/cowork/current-actor");
    const ref = text(payload.ref).trim();
    const identityStatus = text(payload.identity_status);
    if (
      payload.kind !== "human" ||
      ref.length === 0 ||
      (identityStatus !== "local_actor_ref" &&
        identityStatus !== "account_ref")
    ) {
      throw new CoworkHttpError({
        code: "invalid_current_actor_response",
        message:
          "Co-work couldn’t bind this action to the current identity.",
        retryable: true,
      });
    }
    return {
      kind: "human",
      ref,
      identity_status: identityStatus,
    };
  }

  async listFolders(includeIneligible = false): Promise<CoworkFolderListResult> {
    const payload = await this.#json(
      `/api/truth/cowork/folders?include_ineligible=${includeIneligible ? "1" : "0"}`,
    );
    const chooser = record(payload.chooser);
    const available = bool(chooser.available, true);
    return {
      readOnly: bool(payload.read_only ?? payload.readOnly),
      folders: Array.isArray(payload.folders)
        ? payload.folders.map(normalizeFolderSummary).filter((folder) => folder.storeId.length > 0)
        : [],
      diagnostics: Array.isArray(payload.diagnostics) ? payload.diagnostics : [],
      chooser: {
        available,
        kind: text(chooser.kind, "host"),
        importAvailable: bool(
          chooser.import_available ??
            chooser.importAvailable ??
            chooser.markdown_available ??
            chooser.markdownAvailable,
          available,
        ),
        locationAvailable: bool(
          chooser.location_available ?? chooser.locationAvailable,
          available,
        ),
      },
    };
  }

  async chooseFolder(): Promise<CoworkChooseResult> {
    const payload = await this.#json("/api/truth/cowork/folders/choose", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [COWORK_FOLDER_PICKER_INTENT_HEADER]: COWORK_FOLDER_PICKER_INTENT,
      },
      body: "{}",
    });
    return {
      cancelled: bool(payload.cancelled),
      folderName: text(payload.folder_name ?? payload.folderName),
      folderPath: text(payload.folder_path ?? payload.folderPath),
      selectionToken: nullableText(payload.selection_token ?? payload.selectionToken),
    };
  }

  /**
   * @deprecated Compatibility seam for older embedders. The From file
   * workflow uses chooseImportFile and its typed importer identity.
   */
  async chooseMarkdownFile(storeId: string): Promise<CoworkNativePathResult> {
    const payload = await this.#json("/api/truth/cowork/files/choose-markdown", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [COWORK_FOLDER_PICKER_INTENT_HEADER]: COWORK_MARKDOWN_PICKER_INTENT,
      },
      body: JSON.stringify({ store_id: storeId }),
    });
    return normalizeNativePathResult(payload, "Markdown");
  }

  async chooseImportFile(storeId: string): Promise<CoworkImportPathResult> {
    const payload = await this.#json("/api/truth/cowork/files/choose-import", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [COWORK_FOLDER_PICKER_INTENT_HEADER]: COWORK_IMPORT_PICKER_INTENT,
      },
      body: JSON.stringify({ store_id: storeId }),
    });
    const selected = normalizeNativePathResult(payload, "file");
    if (selected.cancelled) {
      return {
        cancelled: true,
        path: "",
        importerId: "",
        mediaType: "",
        sourceSha256: "",
        importer: null,
      };
    }
    const importerId = text(payload.importer_id);
    const mediaType = text(payload.media_type);
    const sourceSha256 = text(payload.source_sha256);
    const importer = normalizeImportDescriptor(payload.importer);
    if (
      importerId.length === 0 ||
      mediaType.length === 0 ||
      !/^[0-9a-f]{64}$/u.test(sourceSha256) ||
      importer === null ||
      importer.importerId !== importerId ||
      importer.mediaType !== mediaType
    ) {
      throw new CoworkHttpError({
        code: "invalid_picker_response",
        message:
          "The file picker did not return a valid, versioned importer descriptor.",
        retryable: true,
      });
    }
    return {
      cancelled: false,
      path: selected.path,
      importerId,
      mediaType,
      sourceSha256,
      importer,
    };
  }

  async chooseLocation(storeId: string): Promise<CoworkNativePathResult> {
    const payload = await this.#json("/api/truth/cowork/folders/choose-location", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [COWORK_FOLDER_PICKER_INTENT_HEADER]: COWORK_LOCATION_PICKER_INTENT,
      },
      body: JSON.stringify({ store_id: storeId }),
    });
    return normalizeNativePathResult(payload, "location");
  }

  async inspectFolder(input: {
    readonly selectionToken?: string;
    readonly folderPath?: string;
    readonly continuationToken?: string;
  }): Promise<CoworkInspectionResult> {
    const body = {
      ...(input.selectionToken === undefined
        ? {}
        : { selection_token: input.selectionToken }),
      ...(input.folderPath === undefined ? {} : { folder_path: input.folderPath }),
      ...(input.continuationToken === undefined
        ? {}
        : { continuation_token: input.continuationToken }),
    };
    const payload = await this.#json("/api/truth/cowork/folders/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const rawCandidate = record(payload.candidate);
    const folderName = text(payload.folder_name ?? rawCandidate.folder_name);
    const folderPath = text(payload.folder_path ?? rawCandidate.folder_path);
    const rawProgress = record(payload.progress);
    const rawBoundaries = Array.isArray(payload.boundaries) ? payload.boundaries : [];
    const rawFolder = payload.folder ?? payload.folder_summary;
    const rawActions = payload.available_actions ?? payload.actions;
    return {
      status: text(payload.status, "unavailable") as CoworkInspectionStatus,
      candidate:
        folderName.length === 0 && folderPath.length === 0
          ? null
          : { folderName, folderPath },
      folder: rawFolder === undefined ? null : normalizeFolderSummary(rawFolder),
      owner: payload.owner === undefined ? null : normalizeFolderSummary(payload.owner),
      boundaries: rawBoundaries.map((entry) => {
        const boundary = record(entry);
        return {
          folderName: text(boundary.folder_name ?? boundary.folderName),
          folderPath: text(boundary.folder_path ?? boundary.folderPath),
          storeId: nullableText(boundary.store_id ?? boundary.storeId),
        };
      }),
      reasonCode: nullableText(payload.reason_code ?? payload.reasonCode),
      actions: Array.isArray(rawActions)
        ? rawActions.filter(
            (entry): entry is string => typeof entry === "string",
          )
        : [],
      inspectionToken: nullableText(payload.inspection_token ?? payload.inspectionToken),
      continuationToken: nullableText(
        payload.continuation_token ?? payload.continuationToken,
      ),
      progress:
        Object.keys(rawProgress).length === 0
          ? null
          : { visited: count(rawProgress.visited), complete: false },
      retryAfterMs:
        typeof (payload.retry_after_ms ?? payload.retryAfterMs) === "number"
          ? Number(payload.retry_after_ms ?? payload.retryAfterMs)
          : null,
    };
  }

  async initializeFolder(inspectionToken: string, idempotencyKey: string) {
    return this.#folderMutation("initialize", inspectionToken, idempotencyKey);
  }

  async openFolder(inspectionToken: string): Promise<CoworkFolderSummary> {
    const payload = await this.#json("/api/truth/cowork/folders/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ inspection_token: inspectionToken }),
    });
    return normalizeFolderSummary(payload.folder ?? payload);
  }

  async #folderMutation(
    operation: "initialize",
    inspectionToken: string,
    idempotencyKey: string,
  ): Promise<CoworkFolderSummary> {
    const payload = await this.#json(`/api/truth/cowork/folders/${operation}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        inspection_token: inspectionToken,
        idempotency_key: idempotencyKey,
      }),
    });
    return normalizeFolderSummary(payload.folder ?? payload);
  }

  async listDocuments(storeId: string): Promise<readonly CoworkDocumentSummary[]> {
    const payload = await this.#json(
      `/api/truth/doc/list?store_id=${encodeURIComponent(storeId)}`,
    );
    const docs = Array.isArray(payload.docs)
      ? payload.docs
      : Array.isArray(payload.documents)
        ? payload.documents
        : [];
    return docs.map(normalizeDocumentSummary).filter((doc) => doc.documentId.length > 0);
  }

  async readDocument(storeId: string, documentId: string): Promise<CoworkDocumentSummary> {
    const payload = await this.#json(
      `/api/truth/doc/${encodeURIComponent(documentId)}?store_id=${encodeURIComponent(storeId)}`,
    );
    return normalizeDocumentSummary(payload.document ?? payload);
  }

  /**
   * Browser-side opening handshake for the server's opaque Yjs bytes. A catalog row saying
   * `ready` is not enough: only the browser can prove the snapshot actually applies and owns
   * the Co-work fragment/fidelity schema. The provider commits activeSession only after this
   * succeeds, so a malformed target never replaces the prior working document.
   */
  async preflightDocument(storeId: string, documentId: string): Promise<void> {
    let pull;
    try {
      pull = await new HttpCoworkYdocTransport({
        storeId,
        documentId,
        fetchImpl: this.#fetch,
      }).pull({});
    } catch (error) {
      throw new CoworkHttpError(
        normalizeCoworkError(
          error,
          undefined,
          "Co-work could not verify the structured document before opening it.",
        ),
      );
    }
    const ydoc = new Y.Doc();
    let editor: Editor | null = null;
    try {
      if (pull.snapshot === null) {
        throw new Error("The canonical structured snapshot is missing.");
      }
      Y.applyUpdate(ydoc, pull.snapshot);
      for (const batch of pull.batches) Y.applyUpdate(ydoc, batch);
      // Mirror the golden bootstrap/read path exactly: apply first, then resolve named roots.
      // Pre-claiming roots before apply changes how Yjs integrates encoded root types and can
      // reject a snapshot produced by bootstrapCoworkYdoc itself.
      const fidelity = ydoc.getMap<unknown>("wb-cowork:fidelity");
      if (fidelity.get("schema") !== "cowork-fidelity/v1") {
        throw new Error("The Co-work fidelity schema is missing or unsupported.");
      }
      void ydoc.getXmlFragment("default");
      // Actually mount the document against the production ProseMirror schema. A Yjs update
      // can be structurally valid yet still contain node content the editor cannot hydrate.
      editor = new Editor({ extensions: buildEditorExtensions(ydoc) });
      void editor.state.doc.content.size;
    } catch (error) {
      throw new CoworkHttpError({
        code: "semantic_corrupt",
        message:
          "This document’s structured state is invalid. Repair it from the Markdown file before opening.",
        retryable: true,
        details: {
          reason: error instanceof Error ? error.message : String(error),
        },
      });
    } finally {
      editor?.destroy();
      ydoc.destroy();
    }
  }

  async listCandidates(
    storeId: string,
    query: string,
    cursor?: string,
  ): Promise<CoworkCandidatesResult> {
    const params = new URLSearchParams({ store_id: storeId, query, limit: "50" });
    if (cursor !== undefined) params.set("cursor", cursor);
    const payload = await this.#json(`/api/truth/doc/candidates?${params.toString()}`);
    const entries = Array.isArray(payload.candidates) ? payload.candidates : [];
    return {
      candidates: entries.map((entry) => {
        const item = record(entry);
        const path = text(item.path ?? item.relative_path);
        const pathParts = path.split("/");
        return {
          path,
          title: text(
            item.title ?? item.display_title,
            pathParts[pathParts.length - 1] ?? path,
          ),
          byteSize: count(item.byte_size ?? item.size),
          modifiedAt: nullableText(item.modified_at ?? item.mtime),
          alreadyRegistered: bool(item.already_registered),
        };
      }),
      cursor: nullableText(payload.next_cursor ?? payload.cursor),
    };
  }

  async prepareBootstrap(
    storeId: string,
    metadata: CoworkBootstrapMetadata,
    source?: Uint8Array,
  ): Promise<CoworkBootstrapPrepared> {
    const form = new FormData();
    // Plain FormData fields land in Flask request.form. A JSON Blob is a multipart file
    // part (request.files), which made otherwise-valid reimport/sitting metadata disappear.
    form.append(
      "metadata",
      JSON.stringify({
        mode: metadata.mode,
        path: metadata.path,
        ...(metadata.title === undefined ? {} : { title: metadata.title }),
        ...(metadata.initialSourceSha256 === undefined
          ? {}
          : { initial_source_sha256: metadata.initialSourceSha256 }),
        expected_file_sha256: metadata.expectedFileSha256 ?? null,
        ...(metadata.importerId === undefined
          ? {}
          : { importer_id: metadata.importerId }),
        ...(metadata.sourceMediaType === undefined
          ? {}
          : { source_media_type: metadata.sourceMediaType }),
        ...(metadata.authorshipAttestation === undefined
          ? {}
          : { authorship_attestation: metadata.authorshipAttestation }),
        document_id: metadata.documentId ?? null,
        idempotency_key: metadata.idempotencyKey,
      }),
    );
    if (source !== undefined) {
      form.append("source", new Blob([source as BlobPart], { type: "application/octet-stream" }));
    }
    const payload = await this.#json(
      `/api/truth/doc/bootstrap?store_id=${encodeURIComponent(storeId)}`,
      { method: "POST", body: form },
    );
    return {
      bootstrapId: text(payload.bootstrap_id),
      documentId: text(payload.document_id),
      mode: text(payload.mode) as CoworkBootstrapPrepared["mode"],
      normalizedPath: text(payload.normalized_path),
      sourceSha256: text(payload.source_sha256),
      sourceByteLength: count(payload.source_byte_length),
      sourceUrl: text(payload.source_url),
      ydocSchema: text(payload.ydoc_schema),
      expiresAt: text(payload.expires_at),
      state: text(payload.state, "prepared") as CoworkBootstrapPrepared["state"],
      result:
        payload.result === undefined || payload.result === null
          ? null
          : normalizeDocumentSummary(payload.result),
    };
  }

  async readBootstrapSource(sourceUrl: string): Promise<Uint8Array> {
    const response = await this.#fetch(sourceUrl, {
      method: "GET",
      credentials: "same-origin",
    });
    if (!response.ok) {
      let payload: unknown = {};
      try {
        payload = await response.json();
      } catch {
        // The typed status fallback still gives the caller a useful recovery state.
      }
      throw new CoworkHttpError(normalizeCoworkError(payload, response.status));
    }
    return new Uint8Array(await response.arrayBuffer());
  }

  async commitBootstrap(
    storeId: string,
    prepared: CoworkBootstrapPrepared,
    snapshot: Uint8Array,
    snapshotSha256: string,
    projection: Uint8Array,
    projectionSha256: string,
  ): Promise<CoworkDocumentSummary> {
    const form = new FormData();
    form.append(
      "metadata",
      JSON.stringify({
        source_sha256: prepared.sourceSha256,
        snapshot_sha256: snapshotSha256,
        projection_sha256: projectionSha256,
        ydoc_schema: prepared.ydocSchema,
      }),
    );
    form.append(
      "snapshot",
      new Blob([snapshot as BlobPart], { type: "application/octet-stream" }),
    );
    form.append(
      "projection",
      new Blob([projection as BlobPart], { type: "text/markdown;charset=utf-8" }),
    );
    const payload = await this.#json(
      `/api/truth/doc/bootstrap/${encodeURIComponent(prepared.bootstrapId)}?store_id=${encodeURIComponent(storeId)}`,
      {
        method: "PUT",
        body: form,
      },
    );
    return normalizeDocumentSummary(payload.document ?? payload);
  }

  async recordPasteProvenance(
    request: CoworkPasteProvenanceRequest,
  ): Promise<CoworkPasteProvenanceReceipt> {
    const payload = await this.#json(
      `/api/truth/doc/${encodeURIComponent(request.documentId)}/authorship-attestations?store_id=${encodeURIComponent(request.storeId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          span: request.anchor,
          attestation: request.attestation,
          basis_kind: request.basisKind,
          expected_structured_head_sha256:
            request.expectedStructuredHeadSha256,
          idempotency_key: request.idempotencyKey,
        }),
      },
    );
    const attestationId = text(payload.attestation_id);
    const documentSpanId = text(payload.document_span_id);
    const targetStructuredHeadSha256 = text(
      payload.target_structured_head_sha256,
    );
    if (
      attestationId.length === 0 ||
      documentSpanId.length === 0 ||
      targetStructuredHeadSha256 !== request.expectedStructuredHeadSha256
    ) {
      throw new CoworkHttpError({
        code: "invalid_provenance_response",
        message:
          "Co-work could not confirm where the pasted text was recorded.",
        retryable: true,
      });
    }
    return {
      attestationId,
      documentSpanId,
      targetStructuredHeadSha256,
    };
  }

  async cancelBootstrap(storeId: string, bootstrapId: string): Promise<void> {
    await this.#json(
      `/api/truth/doc/bootstrap/${encodeURIComponent(bootstrapId)}?store_id=${encodeURIComponent(storeId)}`,
      { method: "DELETE" },
    );
  }

  async inspectDrift(storeId: string, documentId: string): Promise<CoworkDriftInspection> {
    const payload = await this.#json(
      `/api/truth/doc/${encodeURIComponent(documentId)}/drift?store_id=${encodeURIComponent(storeId)}`,
    );
    const rawState = text(payload.state, "clean");
    return {
      state: rawState === "drifted" || rawState === "missing" ? rawState : "clean",
      lastMaterializedSha256: text(payload.last_materialized_sha256),
      currentFileSha256: nullableText(payload.current_file_sha256),
      snapshotSha256: nullableText(payload.snapshot_sha256),
      structuredHeadSha256: nullableText(payload.structured_head_sha256),
      updateTailPresent: bool(payload.update_tail_present),
      unmaterializedStructuredEdits: bool(payload.unmaterialized_structured_edits),
      baseline: normalizeDriftSource(payload.baseline),
      source: normalizeDriftSource(payload.source),
      diffAvailable: bool(payload.diff_available),
      canReimport: bool(payload.can_reimport),
    };
  }

  async readDriftSource(
    source: CoworkDriftSource,
  ): Promise<{ readonly bytes: Uint8Array; readonly etag: string | null }> {
    let response: Response;
    try {
      response = await this.#fetch(source.sourceUrl, {
        method: "GET",
        credentials: "same-origin",
        ...(source.etag === null ? {} : { headers: { "If-Match": source.etag } }),
      });
    } catch (error) {
      throw new CoworkHttpError(normalizeCoworkError(error));
    }
    if (!response.ok) {
      let payload: unknown = {};
      try {
        payload = await response.json();
      } catch {
        // The status still preserves a typed recovery path.
      }
      throw new CoworkHttpError(normalizeCoworkError(payload, response.status));
    }
    const etag = response.headers.get("ETag");
    if (source.etag !== null && etag !== null && etag !== source.etag) {
      throw new CoworkHttpError({
        code: "source_changed",
        message: "The Markdown changed while Co-work was reading the comparison.",
        retryable: true,
      });
    }
    return { bytes: new Uint8Array(await response.arrayBuffer()), etag };
  }

  async prepareReimport(
    storeId: string,
    documentId: string,
    idempotencyKey: string,
  ): Promise<CoworkReimportPrepared> {
    const payload = await this.#json(
      `/api/truth/doc/${encodeURIComponent(documentId)}/reimport?store_id=${encodeURIComponent(storeId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idempotency_key: idempotencyKey }),
      },
    );
    return {
      intentId: text(payload.intent_id),
      state: text(payload.state, "prepared") as CoworkReimportPrepared["state"],
      expiresAt: text(payload.expires_at),
      sourceSha256: text(payload.source_sha256),
      sourceByteLength: count(payload.source_byte_length),
      priorProjectionSha256: text(payload.prior_projection_sha256),
      priorSnapshotSha256: text(payload.prior_snapshot_sha256),
      priorStructuredHeadSha256: text(payload.prior_structured_head_sha256),
      consequence: text(payload.consequence),
      result:
        payload.result === undefined || payload.result === null
          ? null
          : normalizeReimportReceipt(payload.result),
    };
  }

  async readReimportSource(
    storeId: string,
    documentId: string,
    intentId: string,
  ): Promise<Uint8Array> {
    const url = `/api/truth/doc/${encodeURIComponent(documentId)}/reimport/${encodeURIComponent(intentId)}/source?store_id=${encodeURIComponent(storeId)}`;
    let response: Response;
    try {
      response = await this.#fetch(url, { method: "GET", credentials: "same-origin" });
    } catch (error) {
      throw new CoworkHttpError(normalizeCoworkError(error));
    }
    if (!response.ok) {
      let payload: unknown = {};
      try {
        payload = await response.json();
      } catch {
        // Status fallback below.
      }
      throw new CoworkHttpError(normalizeCoworkError(payload, response.status));
    }
    return new Uint8Array(await response.arrayBuffer());
  }

  async commitReimport(
    storeId: string,
    documentId: string,
    prepared: CoworkReimportPrepared,
    snapshot: Uint8Array,
    snapshotSha256: string,
  ): Promise<CoworkReimportReceipt> {
    const form = new FormData();
    form.append("metadata", JSON.stringify({ snapshot_sha256: snapshotSha256 }));
    form.append(
      "snapshot",
      new Blob([snapshot as BlobPart], { type: "application/octet-stream" }),
      "replacement.ydoc",
    );
    const payload = await this.#json(
      `/api/truth/doc/${encodeURIComponent(documentId)}/reimport/${encodeURIComponent(prepared.intentId)}/commit?store_id=${encodeURIComponent(storeId)}`,
      { method: "PUT", body: form },
    );
    return normalizeReimportReceipt(payload);
  }

  async cancelReimport(storeId: string, documentId: string, intentId: string): Promise<void> {
    await this.#json(
      `/api/truth/doc/${encodeURIComponent(documentId)}/reimport/${encodeURIComponent(intentId)}?store_id=${encodeURIComponent(storeId)}`,
      { method: "DELETE" },
    );
  }

  async prepareRetirement(
    storeId: string,
    documentId: string,
    idempotencyKey: string,
  ): Promise<CoworkRetirementPrepared> {
    const payload = await this.#json(
      `/api/truth/doc/${encodeURIComponent(documentId)}/retire?store_id=${encodeURIComponent(storeId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idempotency_key: idempotencyKey }),
      },
    );
    return {
      intentId: text(payload.intent_id),
      expiresAt: text(payload.expires_at),
      documentId: text(payload.document_id),
      consequence: text(payload.consequence),
      consequenceSha256: text(payload.consequence_sha256),
    };
  }

  async commitRetirement(
    storeId: string,
    documentId: string,
    intentId: string,
  ): Promise<CoworkRetirementReceipt> {
    const payload = await this.#json(
      `/api/truth/doc/${encodeURIComponent(documentId)}/retire?store_id=${encodeURIComponent(storeId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ intent_id: intentId }),
      },
    );
    return normalizeRetirementReceipt(payload);
  }
}
