import type { WidgetRendererProps } from "../../dashboard/contributions/contracts";
import { InlineAlert } from "../../ui";
import { createWidgetIntent } from "../shared";
import type {
  MarkdownNoteItem,
  RunningNotesInput,
  RunningNotesIntent,
} from "./contracts";
import {
  MarkdownItemCollection,
  type MarkdownEditRequest,
} from "./MarkdownItemCollection";

export default function RunningNotesWidget({
  input,
  emit,
  presentation,
}: WidgetRendererProps<RunningNotesInput, RunningNotesIntent>) {
  const readOnly = input.access.mode === "read_only";
  const edit = (request: MarkdownEditRequest) => {
    emit(
      createWidgetIntent(presentation, "wb.notes.edit-requested", {
        item_id: request.itemId,
        expected_version: request.expectedVersion,
        markdown: request.markdown,
      }) as RunningNotesIntent,
    );
  };
  const openThread = (item: MarkdownNoteItem) => {
    if (!item.threadId) return;
    emit(
      createWidgetIntent(presentation, "wb.notes.open-thread-requested", {
        item_id: item.itemId,
        thread_id: item.threadId,
      }) as RunningNotesIntent,
    );
  };

  return (
    <div className="wb-running-notes">
      {readOnly && <InlineAlert tone="warning">{input.access.reason}</InlineAlert>}
      {input.items.length === 0 ? (
        <p className="wb-running-notes__empty">No running notes for this collection.</p>
      ) : (
        <MarkdownItemCollection
          items={input.items}
          displayMode={input.displayMode}
          readOnly={readOnly}
          timezone={input.timezone}
          density={presentation.sizeMode}
          onEdit={edit}
          onOpenThread={openThread}
        />
      )}
    </div>
  );
}
