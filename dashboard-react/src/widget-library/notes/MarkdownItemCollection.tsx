import { useEffect, useMemo, useState } from "react";

import { Button, InlineAlert, TextAreaField } from "../../ui";
import { formatTime, ProvenanceBadge, StatusBadge } from "../shared";
import type {
  MarkdownNoteItem,
  NotesDisplayMode,
} from "./contracts";
import "./styles.css";

export interface MarkdownEditRequest {
  readonly itemId: string;
  readonly expectedVersion: number;
  readonly markdown: string;
}

export interface MarkdownItemCollectionProps {
  readonly items: readonly MarkdownNoteItem[];
  readonly displayMode: NotesDisplayMode;
  readonly readOnly: boolean;
  readonly timezone?: string;
  readonly density: "compact" | "standard" | "expanded";
  onEdit(request: MarkdownEditRequest): void;
  onOpenThread?(item: MarkdownNoteItem): void;
}

interface EditSession {
  readonly itemId: string;
  readonly expectedVersion: number;
  readonly draft: string;
}

interface PendingEdit {
  readonly expectedVersion: number;
  readonly markdown: string;
}

const processingTone = (state: MarkdownNoteItem["processing"]["state"]) => {
  if (state === "failed") return "danger" as const;
  if (state === "pending") return "warning" as const;
  if (state === "succeeded") return "success" as const;
  return "neutral" as const;
};

function groupItems(
  items: readonly MarkdownNoteItem[],
  displayMode: NotesDisplayMode,
): readonly { readonly id: string; readonly label: string; readonly items: readonly MarkdownNoteItem[] }[] {
  if (displayMode === "chronological") {
    return [{ id: "all", label: "Notes", items }];
  }
  const groups = new Map<string, MarkdownNoteItem[]>();
  items.forEach((item) => {
    const groupId = item.groupId ?? "ungrouped";
    const group = groups.get(groupId) ?? [];
    group.push(item);
    groups.set(groupId, group);
  });
  return [...groups.entries()].map(([id, groupedItems]) => ({
    id,
    label: id === "ungrouped" ? "Ungrouped" : id,
    items: groupedItems,
  }));
}

export function MarkdownItemCollection({
  items,
  displayMode,
  readOnly,
  timezone,
  density,
  onEdit,
  onOpenThread,
}: MarkdownItemCollectionProps) {
  const [edit, setEdit] = useState<EditSession | null>(null);
  const [pendingEdits, setPendingEdits] = useState<Readonly<Record<string, PendingEdit>>>(
    {},
  );
  const groups = useMemo(() => groupItems(items, displayMode), [displayMode, items]);
  const editingItem =
    edit === null ? undefined : items.find((item) => item.itemId === edit.itemId);
  const conflict =
    edit !== null &&
    editingItem !== undefined &&
    editingItem.version !== edit.expectedVersion;

  useEffect(() => {
    setPendingEdits((current) => {
      const next = Object.fromEntries(
        Object.entries(current).filter(([itemId, pending]) => {
          const item = items.find((candidate) => candidate.itemId === itemId);
          return item === undefined || item.version <= pending.expectedVersion;
        }),
      );
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
  }, [items]);

  const beginEdit = (item: MarkdownNoteItem) => {
    setEdit({
      itemId: item.itemId,
      expectedVersion: item.version,
      draft: item.markdown,
    });
  };
  const save = () => {
    if (edit === null || conflict) return;
    onEdit({
      itemId: edit.itemId,
      expectedVersion: edit.expectedVersion,
      markdown: edit.draft,
    });
    setPendingEdits((current) => ({
      ...current,
      [edit.itemId]: {
        expectedVersion: edit.expectedVersion,
        markdown: edit.draft,
      },
    }));
    setEdit(null);
  };

  return (
    <div className={`wb-markdown-collection wb-markdown-collection--${density}`}>
      {groups.map((group) => (
        <section key={group.id} className="wb-markdown-collection__group">
          {displayMode === "grouped" && <h3>{group.label}</h3>}
          <ol>
            {group.items.map((item) => {
              const isEditing = edit?.itemId === item.itemId;
              const pending = pendingEdits[item.itemId];
              return (
                <li key={item.itemId} className="wb-markdown-item">
                  {isEditing && edit !== null ? (
                    <div className="wb-markdown-item__editor">
                      {conflict && (
                        <InlineAlert tone="warning">
                          This note changed while you were editing. Cancel and reopen it
                          before saving.
                        </InlineAlert>
                      )}
                      <TextAreaField
                        label="Edit note"
                        value={edit.draft}
                        rows={density === "expanded" ? 8 : 5}
                        onChange={(draft) => setEdit({ ...edit, draft })}
                      />
                      <div className="wb-markdown-item__actions">
                        <Button onClick={() => setEdit(null)}>Cancel</Button>
                        <Button variant="primary" disabled={conflict} onClick={save}>
                          Save
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className="wb-markdown-item__content">
                        {pending?.markdown ?? item.markdown}
                      </div>
                      <div className="wb-library-meta-row">
                        <span>{formatTime(item.createdAt, timezone)}</span>
                        <ProvenanceBadge provenance={item.provenance} />
                        <StatusBadge label={item.captureMode} />
                        <StatusBadge
                          label={pending ? "saving" : item.processing.state.replace(/_/g, " ")}
                          tone={pending ? "info" : processingTone(item.processing.state)}
                        />
                        <StatusBadge label={item.resolutionState.replace(/_/g, " ")} />
                      </div>
                      {item.processing.annotation && density !== "compact" && (
                        <InlineAlert tone="info">
                          <strong>{item.processing.annotation.summary}</strong>
                          {item.processing.annotation.effects.length > 0 && (
                            <ul>
                              {item.processing.annotation.effects.map((effect) => (
                                <li key={effect}>{effect}</li>
                              ))}
                            </ul>
                          )}
                        </InlineAlert>
                      )}
                      {item.processing.errorMessage && (
                        <InlineAlert tone="danger">
                          {item.processing.errorMessage}
                        </InlineAlert>
                      )}
                      <div className="wb-markdown-item__actions">
                        <Button
                          variant="ghost"
                          disabled={readOnly || pending !== undefined}
                          onClick={() => beginEdit(item)}
                        >
                          Edit
                        </Button>
                        {item.threadId && onOpenThread && (
                          <Button variant="ghost" onClick={() => onOpenThread(item)}>
                            Open thread
                          </Button>
                        )}
                      </div>
                    </>
                  )}
                </li>
              );
            })}
          </ol>
        </section>
      ))}
    </div>
  );
}
