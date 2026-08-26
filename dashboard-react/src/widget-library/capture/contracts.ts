import type { WidgetIntent } from "../../dashboard/contributions/contracts";
import type { AsyncAnnotation, WidgetAccess } from "../shared";

export type CaptureSubmitMode = "dumb" | "smart";
export type CapturePersistenceStatus = "persisted" | "failed";
export type CaptureProcessingStatus =
  | "not_requested"
  | "pending"
  | "running"
  | "succeeded"
  | "failed";
export type CapturePlacementStatus = "pending" | "placed" | "failed";

/** Navigation is host/provider-authored; inference cannot supply an href. */
export type CaptureFollowUp =
  | { readonly kind: "app_link"; readonly referenceId: string; readonly label: string;
      readonly description?: string; readonly href: string }
  | { readonly kind: "status"; readonly status: "pending" | "failed"; readonly label: string };

export interface CaptureSmartAvailability {
  readonly state: "disabled_by_policy" | "provider_unavailable" | "ready";
  readonly code: string;
  readonly reason: string;
  readonly disclosure: { readonly provider: string | null; readonly model: string | null;
    readonly maxInputBytes: number; readonly tools: false; readonly web: false };
  readonly action?: { readonly kind: "app_link"; readonly label: string; readonly href: string }
    | { readonly kind: "retry"; readonly label: string };
}

export interface CaptureSecondaryAction {
  readonly actionId: string;
  readonly label: string;
  readonly description: string;
  readonly targetId: string;
  readonly mode: CaptureSubmitMode;
}

export interface CaptureTargetOption {
  readonly targetId: string;
  readonly label: string;
  readonly description: string;
  readonly supportedModes: readonly CaptureSubmitMode[];
  readonly defaultMode: CaptureSubmitMode;
  readonly enabled: boolean;
  readonly unavailableReason?: string;
}

export interface CaptureSubmissionRecord {
  readonly captureId?: string;
  readonly clientMutationId: string;
  readonly targetId: string;
  readonly mode: CaptureSubmitMode;
  readonly exactText?: string;
  readonly submittedAt: string;
  readonly persistenceStatus: CapturePersistenceStatus;
  readonly placementStatus?: CapturePlacementStatus;
  readonly processingStatus: CaptureProcessingStatus;
  readonly annotation?: AsyncAnnotation;
  readonly errorMessage?: string;
  readonly sourceRef?: string;
  readonly revision?: number;
  readonly retryable?: boolean;
  readonly followUps?: readonly CaptureFollowUp[];
}

export interface QuickTextCaptureInput {
  readonly instanceId: string;
  readonly revision: string;
  readonly dayId: string;
  readonly access: WidgetAccess;
  /** `view` means a containing surface already renders the access notice. */
  readonly accessNotice?: "widget" | "view";
  readonly smartHelp?: {
    readonly summary: string;
    readonly details: string;
  };
  readonly smartAvailability?: CaptureSmartAvailability;
  readonly secondaryActions?: readonly CaptureSecondaryAction[];
  readonly targets: readonly CaptureTargetOption[];
  readonly capturesToday: number;
  readonly recentSubmissions: readonly CaptureSubmissionRecord[];
}

export interface CaptureDraftRequest {
  readonly clientMutationId: string;
  readonly dayId: string;
  readonly targetId: string;
  readonly mode: CaptureSubmitMode;
  readonly exactText: string;
  readonly statedAt?: string;
  readonly followUpActionId?: string;
  readonly smartDisclosureSha256?: string;
}

export interface CaptureSubmitIntent
  extends WidgetIntent<{
    readonly day_id: string;
    readonly target_id: string;
    readonly mode: CaptureSubmitMode;
    readonly exact_text: string;
    readonly stated_at?: string;
    readonly follow_up_action?: string;
    readonly smart_disclosure_sha256?: string;
  }> {
  readonly intent_type: "wb.capture.submit";
  readonly client_mutation_id: string;
}

export interface CaptureRetryIntent extends WidgetIntent<{
  readonly capture_id: string;
  readonly expected_revision: number;
  readonly smart_disclosure_sha256?: string;
}> { readonly intent_type: "wb.capture.retry-requested" }

export interface CaptureAvailabilityIntent extends WidgetIntent<Record<string, never>> {
  readonly intent_type: "wb.capture.availability-refresh";
}

export type CaptureIntent = CaptureSubmitIntent | CaptureRetryIntent | CaptureAvailabilityIntent;
