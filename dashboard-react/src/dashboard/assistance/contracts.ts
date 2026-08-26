import type { JsonObject, JsonSchemaReference, JsonValue } from "../contributions/contracts";
import type { WidgetDraftIdentity } from "../drafts/contracts";

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
  readonly assistantSessionId: string;
  readonly conversationId: string;
  readonly identity: WidgetDraftIdentity;
  readonly schema: JsonSchemaReference;
  readonly expiresAt: string;
  readonly availability: AssistanceAvailability;
}

export interface PreparedDraftSnapshot {
  readonly messageId: string;
  readonly baseDraftRevision: number;
  readonly baseSnapshotHash: string;
  readonly snapshot: JsonObject;
}
