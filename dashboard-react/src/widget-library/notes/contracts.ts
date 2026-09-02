import type { WidgetIntent } from "../../dashboard/contributions/contracts";
import type { AsyncAnnotation, WidgetAccess, WidgetProvenance } from "../shared";
import type { CaptureFollowUp } from "../capture/contracts";

export type NoteProcessingState =
  | "not_requested"
  | "pending"
  | "running"
  | "succeeded"
  | "failed";
export type NoteResolutionState =
  | "open"
  | "routed_to_task"
  | "routed_to_consideration"
  | "appended"
  | "dismissed";
export type NotesDisplayMode = "chronological" | "grouped";

export interface NoteProcessing {
  readonly state: NoteProcessingState;
  readonly annotation?: AsyncAnnotation;
  readonly errorMessage?: string;
}

export type NoteDocumentState =
  | {
      readonly state: "available";
      readonly gestureContextSha256: string;
    }
  | {
      readonly state: "current" | "paused_diverged";
      readonly gestureContextSha256: string;
      readonly href: string;
      readonly storeId: string;
      readonly documentId: string;
      readonly changeId: string;
      readonly contentAuthorityEpoch: number;
    };

export interface MarkdownNoteItem {
  readonly itemId: string;
  readonly markdown: string;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly provenance: WidgetProvenance;
  readonly captureMode: "dumb" | "smart";
  readonly processing: NoteProcessing;
  readonly resolutionState: NoteResolutionState;
  readonly groupId?: string;
  readonly threadId?: string;
  readonly version: number;
  readonly document?: NoteDocumentState;
  readonly followUps?: readonly CaptureFollowUp[];
}

export interface RunningNotesInput {
  readonly instanceId: string;
  readonly revision: string;
  readonly dayId: string;
  readonly timezone?: string;
  readonly access: WidgetAccess;
  readonly displayMode: NotesDisplayMode;
  readonly items: readonly MarkdownNoteItem[];
  readonly supplementalItems?: readonly {
    readonly itemId: string;
    readonly itemKind: string;
    readonly text: string;
    readonly authorityKind: string;
  }[];
  readonly tombstones?: readonly MarkdownNoteItem[];
}

export interface NoteEditRequestedIntent
  extends WidgetIntent<{
    readonly item_id: string;
    readonly expected_version: number;
    readonly markdown: string;
  }> {
  readonly intent_type: "wb.notes.edit-requested";
  readonly client_mutation_id: string;
}

export interface NoteDeleteRequestedIntent
  extends WidgetIntent<{
    readonly item_id: string;
    readonly expected_version: number;
  }> {
  readonly intent_type: "wb.notes.delete-requested";
  readonly client_mutation_id: string;
}

export interface NoteRestoreRequestedIntent
  extends WidgetIntent<{
    readonly item_id: string;
    readonly expected_version: number;
  }> {
  readonly intent_type: "wb.notes.restore-requested";
  readonly client_mutation_id: string;
}

export interface NoteOpenThreadRequestedIntent
  extends WidgetIntent<{
    readonly item_id: string;
    readonly thread_id: string;
  }> {
  readonly intent_type: "wb.notes.open-thread-requested";
}

export interface NoteOpenDocumentRequestedIntent
  extends WidgetIntent<{
    readonly item_id: string;
    readonly expected_version: number;
    readonly gesture_context_sha256: string;
  }> {
  readonly intent_type: "wb.notes.open-document-requested";
}

export type RunningNotesIntent =
  | NoteEditRequestedIntent
  | NoteDeleteRequestedIntent
  | NoteRestoreRequestedIntent
  | NoteOpenThreadRequestedIntent
  | NoteOpenDocumentRequestedIntent;
