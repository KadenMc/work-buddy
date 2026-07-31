/** JSON-only contracts shared by the Co-work provider and its durable widget. */

export type CoworkDriftState = "clean" | "drifted" | "missing";

export type CoworkInitializationState =
  | "ready"
  | "bootstrap_required"
  | "updates_without_snapshot"
  | "corrupt"
  | "semantic_corrupt";

export interface CoworkDocumentPermissions {
  readonly open: boolean;
  readonly edit: boolean;
  readonly materialize: boolean;
  readonly repair: boolean;
  readonly retire: boolean;
}

export interface CoworkDocumentSummary {
  readonly documentId: string;
  readonly path: string;
  readonly title: string;
  readonly profile: string;
  readonly documentClass?: string;
  readonly sourceWriteback?: "same_file" | "never";
  readonly lifecycle?: "active" | "retired";
  readonly initializationState?: CoworkInitializationState;
  readonly structuredHeadSha256?: string | null;
  readonly snapshotSha256?: string | null;
  readonly projectionSha256?: string | null;
  readonly currentFileSha256?: string | null;
  /** Exact source bytes captured when a detached From file document was created. */
  readonly importSourceSha256?: string | null;
  /** Source file bytes currently observed at the import path, when still readable. */
  readonly observedSourceFileSha256?: string | null;
  readonly projectionBlobAvailable?: boolean;
  readonly driftState: CoworkDriftState;
  readonly openProposalCount: number;
  readonly openFlagCount: number;
  readonly updatedAt?: string | null;
  readonly permissions?: CoworkDocumentPermissions;
  readonly disabledReason?: string | null;
}

/**
 * Source-file writes are an explicit two-part capability. Legacy server
 * payloads are normalized before reaching this boundary; unnormalized or
 * malformed documents fail closed.
 */
export const coworkDocumentCanWriteBackSource = (
  document: CoworkDocumentSummary,
): boolean =>
  document.sourceWriteback === "same_file" &&
  document.permissions?.materialize === true;

export interface CoworkApiError {
  readonly code: string;
  readonly message: string;
  readonly field?: string;
  readonly retryable: boolean;
  readonly details?: Readonly<Record<string, unknown>>;
  readonly status?: number;
}

export interface CoworkFolderPermissions {
  readonly read: boolean;
  readonly create: boolean;
  readonly import: boolean;
  readonly materialize: boolean;
  readonly retire: boolean;
}

export interface CoworkFolderChooserAvailability {
  readonly available: boolean;
  readonly kind: string;
  readonly importAvailable: boolean;
  readonly locationAvailable: boolean;
}

export interface CoworkFolderChooserInput {
  readonly available: boolean;
  readonly kind: string;
  readonly importAvailable?: boolean;
  readonly locationAvailable?: boolean;
}

export interface CoworkDocumentSurfacePolicy {
  readonly enabled: boolean;
  readonly allowedDocumentClasses: readonly string[];
  readonly feedbackCapture: boolean;
}

export interface CoworkFolderCandidate {
  readonly folderName: string;
  readonly folderPath: string;
}

export interface CoworkFolderSummary extends CoworkFolderCandidate {
  readonly storeId: string;
  readonly layout: string;
  readonly reachable: boolean;
  readonly eligibility: string;
  readonly ineligibleReason: string | null;
  readonly documentSurface: CoworkDocumentSurfacePolicy;
  readonly permissions: CoworkFolderPermissions;
  readonly documentCount: number;
}

export interface CoworkFolderBoundarySummary extends CoworkFolderCandidate {
  readonly storeId: string | null;
}

export type CoworkFolderConflictCode =
  | "folder_layout_incomplete"
  | "folder_store_collision"
  | "identity_conflict";

export type CoworkFolderUnavailableCode =
  | "folder_not_found"
  | "folder_unreadable"
  | "folder_disallowed"
  | "descendant_scan_incomplete"
  | "folder_too_large_for_safe_setup";

export type CoworkFolderAction =
  | "retry"
  | "inspect"
  | "open_owner"
  | "choose_another";

export type CoworkFolderSelection =
  | { readonly kind: "none" }
  | { readonly kind: "choosing" }
  | {
      readonly kind: "inspecting";
      readonly candidate: CoworkFolderCandidate | null;
    }
  | {
      readonly kind: "inspecting_descendants";
      readonly candidate: CoworkFolderCandidate;
      readonly progress: { readonly visited: number; readonly complete: false };
    }
  | { readonly kind: "initialized"; readonly folder: CoworkFolderSummary }
  | {
      readonly kind: "setup_confirmation";
      readonly candidate: CoworkFolderCandidate;
    }
  | {
      readonly kind: "setup_available";
      readonly candidate: CoworkFolderCandidate;
    }
  | {
      readonly kind: "inside_existing_folder";
      readonly candidate: CoworkFolderCandidate;
      readonly owner: CoworkFolderSummary;
    }
  | {
      readonly kind: "contains_nested_folder";
      readonly candidate: CoworkFolderCandidate;
      readonly boundaries: readonly CoworkFolderBoundarySummary[];
    }
  | {
      readonly kind: "store_layout_conflict";
      readonly candidate: CoworkFolderCandidate;
      readonly reasonCode: CoworkFolderConflictCode;
      readonly availableActions: readonly CoworkFolderAction[];
    }
  | {
      readonly kind: "unavailable";
      readonly candidate: CoworkFolderCandidate | null;
      readonly reasonCode: CoworkFolderUnavailableCode;
      readonly retryable: boolean;
    };

export type CoworkCatalogStatus =
  | "loading"
  | "ready"
  | "empty"
  | "unreachable"
  | "disabled"
  | "read-only"
  | "error";

export interface CoworkCatalogState {
  readonly status: CoworkCatalogStatus;
  readonly documents: readonly CoworkDocumentSummary[];
  readonly refreshedAt: string | null;
  readonly error: CoworkApiError | null;
}

export type CoworkRouteTarget =
  | { readonly kind: "launcher"; readonly storeId: string | null }
  | { readonly kind: "scratch"; readonly scratchId: string; readonly title: string }
  | {
      readonly kind: "registered";
      readonly storeId: string;
      readonly documentId: string;
    }
  | {
      readonly kind: "unavailable";
      readonly storeId: string;
      readonly documentId: string;
      readonly reason: string;
    };

export type CoworkActiveSession =
  | { readonly kind: "none" }
  | { readonly kind: "scratch"; readonly scratchId: string; readonly title: string }
  | {
      readonly kind: "registered";
      readonly storeId: string;
      readonly document: CoworkDocumentSummary;
    };

export interface CoworkScratchSummary {
  readonly scratchId: string;
  readonly title: string;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly recoveredFromPreviousEditor: boolean;
}

/** Coarse authoritative provider state. Binary Y.Doc data never enters this model. */
export interface CoworkViewModel {
  readonly folders: readonly CoworkFolderSummary[];
  readonly folderChooser: CoworkFolderChooserAvailability;
  readonly folderSelection: CoworkFolderSelection;
  readonly activeFolderStoreId: string | null;
  readonly catalog: CoworkCatalogState;
  readonly scratches: readonly CoworkScratchSummary[];
  readonly routeTarget: CoworkRouteTarget;
  readonly activeSession: CoworkActiveSession;
  readonly openingTarget: CoworkRouteTarget | null;
  readonly navigationError: CoworkApiError | null;
  readonly readOnly: boolean;
  /** Shallow projection used by fixture and demo providers. */
  readonly document: CoworkDocumentSummary | null;
}

/**
 * JSON projection hydrated into the one durable workspace widget. Fields after document and
 * sessionQuality are optional so minimal Widget Lab and demo providers can supply a shallow
 * projection; HttpCoworkProvider always supplies the complete projection.
 */
export interface CoworkWorkspaceInput {
  readonly document: CoworkDocumentSummary | null;
  readonly sessionQuality: string;
  readonly folders?: readonly CoworkFolderSummary[];
  readonly folderChooser?: CoworkFolderChooserInput;
  readonly folderSelection?: CoworkFolderSelection;
  readonly activeFolderStoreId?: string | null;
  readonly catalog?: CoworkCatalogState;
  readonly scratches?: readonly CoworkScratchSummary[];
  readonly routeTarget?: CoworkRouteTarget;
  readonly activeSession?: CoworkActiveSession;
  readonly openingTarget?: CoworkRouteTarget | null;
  readonly navigationError?: CoworkApiError | null;
  readonly readOnly?: boolean;
}

export interface CoworkFolderSelectIntentPayload {
  readonly action:
    | "choose"
    | "inspect"
    | "continue"
    | "initialize"
    | "open"
    | "retry"
    | "cancel";
  readonly folderPath?: string;
  readonly storeId?: string;
}

export interface CoworkDocumentOpenIntentPayload {
  readonly storeId: string;
  readonly documentId: string;
}

export interface CoworkScratchOpenIntentPayload {
  readonly scratchId?: string;
  readonly title?: string;
}

export interface CoworkScratchCloseIntentPayload {
  /**
   * A promotion retires only the named scratch metadata. It deliberately does not close
   * the newly opened registered session; the provider accepts this form only after that
   * session is active.
   */
  readonly retire?: boolean;
  readonly scratchId?: string;
}

export interface CoworkScratchTouchIntentPayload {
  readonly scratchId: string;
}

export const COWORK_INTENTS = {
  folderSelect: "wb.cowork.folder.select",
  folderClose: "wb.cowork.folder.close",
  catalogRefresh: "wb.cowork.catalog.refresh",
  documentOpen: "wb.cowork.document.open",
  documentReload: "wb.cowork.document.reload",
  documentClose: "wb.cowork.document.close",
  scratchOpen: "wb.cowork.scratch.open",
  scratchClose: "wb.cowork.scratch.close",
  scratchTouch: "wb.cowork.scratch.touch",
} as const;
