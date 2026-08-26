import type { JsonObject, JsonSchemaReference, JsonValue } from "../contributions/contracts";
import type { WidgetDraftIdentity } from "../drafts/contracts";
import type { ChatExecutionSnapshot } from "../../widget-library/chat";

export interface AssistedField {
  readonly path: readonly string[];
  readonly type: "string" | "number" | "boolean";
  readonly enum?: readonly JsonValue[];
  readonly maxLength?: number;
  readonly pattern?: string;
  readonly minimum?: number;
  readonly maximum?: number;
  readonly required: boolean;
  readonly description: string;
  readonly sensitivity: "ordinary" | "private" | "secret";
  readonly disclosure: "explicit_start" | "never";
  readonly default: JsonValue;
  readonly contentPolicy?: "non_secret_json";
}

export interface AssistedFormSchema {
  readonly schema: JsonSchemaReference;
  readonly title: string;
  readonly submitPolicy: "user_only";
  readonly patchBehavior: "suggest" | "apply_when_uncontested";
  readonly allowedOperations: readonly ("set" | "remove")[];
  readonly maxOperations: number;
  readonly maxPatchBytes: number;
  readonly maxSnapshotBytes: number;
  readonly referenceScopes?: readonly ("job_capability" | "job_workflow")[];
  readonly fields: readonly AssistedField[];
}

export type DraftPatchOperation =
  | { readonly op: "set"; readonly path: readonly string[]; readonly value: JsonValue }
  | { readonly op: "remove"; readonly path: readonly string[] };

export interface AssistedDraftPatch {
  readonly protocol: "wb.assisted-draft.patch/v1";
  readonly assistantSessionId: string;
  readonly conversationId: string;
  readonly identity: WidgetDraftIdentity;
  readonly schema: JsonSchemaReference;
  readonly baseDraftRevision: number;
  readonly baseSnapshotHash: string;
  readonly baseSnapshot: JsonObject;
  readonly patchId: string;
  readonly operations: readonly DraftPatchOperation[];
}

export interface DraftPatchReceipt {
  readonly patchId: string;
  readonly status: "applied" | "pending" | "partial" | "rejected" | "undone";
  readonly appliedFields: readonly (readonly string[])[];
  readonly pendingFields: readonly { readonly path: readonly string[]; readonly reason: "focused" | "user_changed" | "suggest_only" | "storage_conflict" }[];
  readonly resultingRevision: number;
  readonly message: string;
}

export interface AssistanceAvailability {
  readonly available: boolean;
  readonly code: "ready" | "not_configured" | "disabled" | "invalid_configuration" | "unsupported_provider" | "provider_unavailable";
  readonly message: string;
  readonly providerId?: string;
  readonly modelId?: string;
  readonly purpose: "dashboard.assisted_draft";
  readonly disclosure: string;
}

export interface AssistanceSession {
  readonly protocol?: string;
  readonly assistantSessionId: string;
  readonly conversationId: string;
  readonly identity: WidgetDraftIdentity;
  readonly schema: JsonSchemaReference;
  readonly expiresAt: string;
  readonly availability: AssistanceAvailability;
  readonly phase?: AssistancePhase;
  readonly activeStartId?: string | null;
  readonly controlRevision?: number;
  readonly execution?: ChatExecutionSnapshot;
  readonly agent?: AssistanceAgent;
}

export type AssistancePhase = "prepared" | "starting" | "active" | "stopped" | "ended" | "expired" | "restart_required";

export interface AssistanceAgent {
  readonly status: string;
  readonly alive?: boolean | null;
  readonly started?: boolean;
  readonly error?: string | null;
  readonly phase?: AssistancePhase;
  readonly activeStartId?: string | null;
  readonly controlRevision?: number;
}

export interface AssistanceStartRequest {
  readonly requestId: string;
  readonly disclosureAccepted: true;
  readonly provider_id: string;
  readonly model_id: string;
  readonly expected_revision: string;
  readonly expected_control_revision: number;
  readonly initialSnapshot: PreparedDraftSnapshot;
}

export interface AssistanceStopRequest {
  readonly requestId: string;
  readonly expected_control_revision: number;
  readonly startRequestId?: string;
}

export interface AssistanceStopResult {
  readonly stopped: true;
  readonly controlRevision?: number;
  /** already_absent is the client's typed-404 acknowledgement, not a fabricated server revision. */
  readonly outcome: "stopped" | "superseded" | "already_absent";
}

export interface PreparedDraftSnapshot {
  readonly messageId: string;
  readonly baseDraftRevision: number;
  readonly baseSnapshotHash: string;
  readonly snapshot: JsonObject;
}
