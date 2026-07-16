import type { WidgetIntent } from "../../dashboard/contributions/contracts";
import type { AsyncAnnotation, WidgetAccess, WidgetProvenance } from "../shared";

export type NoteProcessingState = "not_requested" | "pending" | "succeeded" | "failed";
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
}

export interface RunningNotesInput {
  readonly instanceId: string;
  readonly revision: string;
  readonly dayId: string;
  readonly timezone?: string;
  readonly access: WidgetAccess;
  readonly displayMode: NotesDisplayMode;
  readonly items: readonly MarkdownNoteItem[];
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

export interface NoteOpenThreadRequestedIntent
  extends WidgetIntent<{
    readonly item_id: string;
    readonly thread_id: string;
  }> {
  readonly intent_type: "wb.notes.open-thread-requested";
}

export type RunningNotesIntent =
  | NoteEditRequestedIntent
  | NoteDeleteRequestedIntent
  | NoteOpenThreadRequestedIntent;
