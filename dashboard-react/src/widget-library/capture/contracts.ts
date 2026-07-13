import type { WidgetIntent } from "../../dashboard/contributions/contracts";
import type { AsyncAnnotation, WidgetAccess } from "../shared";

export type CaptureSubmitMode = "dumb" | "smart";
export type CapturePersistenceStatus = "persisted" | "failed";
export type CaptureProcessingStatus =
  | "not_requested"
  | "pending"
  | "succeeded"
  | "failed";

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
  readonly clientMutationId: string;
  readonly targetId: string;
  readonly mode: CaptureSubmitMode;
  readonly exactText: string;
  readonly submittedAt: string;
  readonly persistenceStatus: CapturePersistenceStatus;
  readonly processingStatus: CaptureProcessingStatus;
  readonly annotation?: AsyncAnnotation;
  readonly errorMessage?: string;
}

export interface QuickTextCaptureInput {
  readonly instanceId: string;
  readonly revision: string;
  readonly dayId: string;
  readonly access: WidgetAccess;
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
}

export interface CaptureSubmitIntent
  extends WidgetIntent<{
    readonly day_id: string;
    readonly target_id: string;
    readonly mode: CaptureSubmitMode;
    readonly exact_text: string;
    readonly stated_at?: string;
  }> {
  readonly intent_type: "wb.capture.submit";
  readonly client_mutation_id: string;
}
