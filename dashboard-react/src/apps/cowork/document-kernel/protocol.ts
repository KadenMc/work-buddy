export const DOCUMENT_KERNEL_PROTOCOL = "cowork-document-kernel/v1";
export const DOCUMENT_KERNEL_RUNTIME_VERSION = "1.0.0";
export const DOCUMENT_KERNEL_MAX_REQUEST_BYTES = 16 * 1024 * 1024;
export const DOCUMENT_KERNEL_MAX_SEGMENT_BYTES = 64 * 1024 * 1024;

export interface KernelTextSelector {
  readonly kind: "prosemirror_text/v1";
  readonly from: number;
  readonly to: number;
  readonly expectedText: string;
}

export type KernelOperation =
  | { readonly kind: "health" }
  | {
      readonly kind: "bootstrap_markdown";
      readonly sourceBase64: string;
      readonly sourceSha256: string;
      readonly newlineStyle: "crlf" | "lf" | "cr" | "none";
      readonly utf8Bom: boolean;
      readonly trailingNewlineCount: number;
    }
  | {
      readonly kind: "project_markdown";
      readonly snapshotBase64: string;
      readonly updatesBase64: readonly string[];
      readonly expectedBaseStructuredHeadSha256: string;
    }
  | {
      readonly kind: "apply_source_markdown";
      readonly snapshotBase64: string;
      readonly updatesBase64: readonly string[];
      readonly expectedBaseStructuredHeadSha256: string;
      readonly sourceBase64: string;
      readonly sourceSha256: string;
      readonly newlineStyle: "crlf" | "lf" | "cr" | "none";
      readonly utf8Bom: boolean;
      readonly trailingNewlineCount: number;
    }
  | {
      readonly kind: "replace_text";
      readonly snapshotBase64: string;
      readonly updatesBase64: readonly string[];
      readonly expectedBaseStructuredHeadSha256: string;
      readonly selector: KernelTextSelector;
      readonly copiedText: string;
      readonly copiedTextSha256: string;
    }
  | {
      readonly kind: "validate_yjs_update";
      readonly snapshotBase64: string;
      readonly updatesBase64: readonly string[];
      readonly expectedBaseStructuredHeadSha256: string;
      readonly updateBase64: string;
      readonly expectedResultStructuredHeadSha256: string;
    };

export interface KernelRequest {
  readonly protocol: typeof DOCUMENT_KERNEL_PROTOCOL;
  readonly runtimeVersion: typeof DOCUMENT_KERNEL_RUNTIME_VERSION;
  readonly schemaVersion: "cowork-yjs/v1";
  readonly requestId: string;
  readonly deadlineMs: number;
  readonly operation: KernelOperation;
}

export interface KernelResponse {
  readonly protocol: typeof DOCUMENT_KERNEL_PROTOCOL;
  readonly runtimeVersion: typeof DOCUMENT_KERNEL_RUNTIME_VERSION;
  readonly schemaVersion: "cowork-yjs/v1";
  readonly requestId: string;
  readonly operationKind: KernelOperation["kind"];
  readonly ok: boolean;
  readonly result?: Readonly<Record<string, unknown>>;
  readonly error?: {
    readonly code: string;
    readonly retryable: boolean;
  };
}

export class DocumentKernelError extends Error {
  constructor(
    readonly code: string,
    readonly retryable = false,
  ) {
    super(code);
    this.name = "DocumentKernelError";
  }
}
