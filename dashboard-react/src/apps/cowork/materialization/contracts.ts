import type { CoworkApiError } from "../contracts";

export interface CoworkMaterializeRequest {
  readonly renderedMarkdown: string;
  readonly renderedSha256: string;
  readonly expectedFileSha256: string;
  readonly expectedStructuredHeadSha256: string;
  readonly snapshotSha256: string;
  readonly idempotencyKey: string;
}

export interface CoworkMaterializeReceipt {
  readonly newFileSha256: string;
  readonly structuredHeadSha256: string;
  readonly documentVersionId: string;
  readonly materializedAt: string;
  readonly driftState: "clean";
}

export type CoworkMaterializationState =
  | { readonly kind: "checking" }
  | { readonly kind: "up_to_date"; readonly fileSha256: string }
  | { readonly kind: "unsaved"; readonly fileSha256: string }
  | { readonly kind: "saving"; readonly fileSha256: string }
  | {
      readonly kind: "conflict";
      readonly fileSha256: string | null;
      readonly error: CoworkApiError;
      readonly canRetry: boolean;
    }
  | {
      readonly kind: "error";
      readonly fileSha256: string | null;
      readonly error: CoworkApiError;
      readonly canRetry: boolean;
    }
  | { readonly kind: "read_only"; readonly reason: string };

export interface CoworkMaterializationController {
  save(): Promise<void>;
  retrySync(): Promise<void>;
  /**
   * Persist and compact the live structured document before a lifecycle
   * operation. This must never materialize or otherwise write the source file.
   */
  settleForLifecycle(): Promise<void>;
}
