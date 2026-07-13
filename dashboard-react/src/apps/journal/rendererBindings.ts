import type { QuickTextCaptureInput } from "../../widget-library/capture/contracts";
import type { RunningNotesInput } from "../../widget-library/notes/contracts";
import type { DayTimelineInput } from "../../widget-library/timeline/contracts";
import type {
  JournalCaptureInput,
  JournalRunningNotesInput,
  JournalTimelineInput,
} from "./contracts";

/**
 * Journal-to-library presentation bindings.
 *
 * These identity functions intentionally contain no Journal behavior. Their return
 * types are compile-time conformance checks: Journal owns data semantics, while the
 * reusable libraries own renderer-facing structural contracts.
 */
export const toQuickTextCaptureInput = (
  input: JournalCaptureInput,
): QuickTextCaptureInput => input;

export const toDayTimelineInput = (input: JournalTimelineInput): DayTimelineInput =>
  input;

export const toRunningNotesInput = (
  input: JournalRunningNotesInput,
): RunningNotesInput => input;
