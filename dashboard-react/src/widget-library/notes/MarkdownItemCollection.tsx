import { useEffect, useMemo, useState } from "react";

import type { IntentResult } from "../../dashboard/contributions/contracts";
import { useWidgetDraft } from "../../dashboard/drafts";
import { useInteractionSurfaces } from "../../dashboard/interactions";
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

export interface MarkdownDeleteRequest {
  readonly itemId: string;
  readonly expectedVersion: number;
}

export interface MarkdownItemCollectionProps {
  readonly items: readonly MarkdownNoteItem[];
  readonly displayMode: NotesDisplayMode;
  readonly readOnly: boolean;
  readonly timezone?: string;
  readonly density: "compact" | "standard" | "expanded";
  onEdit(request: MarkdownEditRequest): Promise<IntentResult> | IntentResult | void;
  onDelete(request: MarkdownDeleteRequest): Promise<IntentResult> | IntentResult | void;
  onOpenThread?(item: MarkdownNoteItem): void;
  /** Preview may demonstrate the removal locally; Dashboard Core still blocks persistence. */
  readonly simulateMutations?: boolean;
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
  onDelete,
  onOpenThread,
  simulateMutations = false,
}: MarkdownItemCollectionProps) {
  const { confirm } = useInteractionSurfaces();
  const editDraft = useWidgetDraft<EditSession | null>("edit", null, {
    isPristine: (value) => value === null,
  });
  const edit = editDraft.value;
  const setEdit = editDraft.setValue;
  const [saveError, setSaveError] = useState<string>();
  const [deleteError, setDeleteError] = useState<{
    readonly itemId: string;
    readonly message: string;
  }>();
  const [pendingEdits, setPendingEdits] = useState<Readonly<Record<string, PendingEdit>>>(
    {},
  );
  const [pendingDeletes, setPendingDeletes] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const [previewDeletedItemIds, setPreviewDeletedItemIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const visibleItems = useMemo(
    () => items.filter((item) => !previewDeletedItemIds.has(item.itemId)),
    [items, previewDeletedItemIds],
  );
  const groups = useMemo(
    () => groupItems(visibleItems, displayMode),
    [displayMode, visibleItems],
  );
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

  useEffect(() => {
    const visibleItemIds = new Set(items.map((item) => item.itemId));
    setPendingDeletes((current) => {
      const next = new Set([...current].filter((itemId) => visibleItemIds.has(itemId)));
      return next.size === current.size ? current : next;
    });
  }, [items]);

  const beginEdit = (item: MarkdownNoteItem) => {
    setSaveError(undefined);
    setDeleteError(undefined);
    setEdit({
      itemId: item.itemId,
      expectedVersion: item.version,
      draft: item.markdown,
    });
  };
  const save = async () => {
    if (edit === null || conflict) return;
    const submittedRevision = editDraft.revision;
    try {
      await editDraft.flush();
    } catch {
      return;
    }
    const result = await onEdit({
      itemId: edit.itemId,
      expectedVersion: edit.expectedVersion,
      markdown: edit.draft,
    });
    if (result !== undefined && result.status !== "accepted") {
      setSaveError(result.message ?? `The note update was ${result.status}.`);
      return;
    }
    setPendingEdits((current) => ({
      ...current,
      [edit.itemId]: {
        expectedVersion: edit.expectedVersion,
        markdown: edit.draft,
      },
    }));
    setSaveError(undefined);
    await editDraft.clear({ ifRevision: submittedRevision });
  };
  const requestDelete = async (item: MarkdownNoteItem) => {
    setDeleteError(undefined);
    const accepted = await confirm({
      title: "Delete this running note?",
      description:
        "It will leave the active Running Notes list. Work Buddy keeps a tombstone so history and downstream context remain accurate.",
      confirmLabel: "Delete note",
      cancelLabel: "Keep note",
      tone: "danger",
    });
    if (!accepted) return;
    const result = await onDelete({
      itemId: item.itemId,
      expectedVersion: item.version,
    });
    if (result !== undefined && result.status !== "accepted") {
      setDeleteError({
        itemId: item.itemId,
        message: result.message ?? `The note deletion was ${result.status}.`,
      });
      return;
    }
    if (simulateMutations) {
      setPreviewDeletedItemIds((current) => new Set(current).add(item.itemId));
      return;
    }
    setPendingDeletes((current) => new Set(current).add(item.itemId));
  };

  if (!editDraft.ready) {
    return <p className="wb-markdown-collection__draft-loading" aria-busy="true">Restoring draft…</p>;
  }

  if (visibleItems.length === 0) {
    return (
      <p className="wb-running-notes__empty">
        No active running notes for this collection.
      </p>
    );
  }

  return (
    <div className={`wb-markdown-collection wb-markdown-collection--${density}`}>
      {groups.map((group) => (
        <section key={group.id} className="wb-markdown-collection__group">
          {displayMode === "grouped" && <h3>{group.label}</h3>}
          <ol>
            {group.items.map((item) => {
              const isEditing = edit?.itemId === item.itemId;
              const pending = pendingEdits[item.itemId];
              const deleting = pendingDeletes.has(item.itemId);
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
                      {editDraft.error ? (
                        <InlineAlert tone="danger">{editDraft.error}</InlineAlert>
                      ) : null}
                      {saveError ? <InlineAlert tone="danger">{saveError}</InlineAlert> : null}
                      <TextAreaField
                        label="Edit note"
                        value={edit.draft}
                        rows={density === "expanded" ? 8 : 5}
                        onChange={(draft) => setEdit({ ...edit, draft })}
                      />
                      <div className="wb-markdown-item__actions">
                        <Button onClick={() => void editDraft.clear()}>Cancel</Button>
                        <Button variant="primary" disabled={conflict} onClick={() => void save()}>
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
                      {deleteError?.itemId === item.itemId && (
                        <InlineAlert tone="danger">{deleteError.message}</InlineAlert>
                      )}
                      <div className="wb-markdown-item__actions">
                        <Button
                          variant="ghost"
                          disabled={readOnly || pending !== undefined || deleting}
                          onClick={() => beginEdit(item)}
                        >
                          Edit
                        </Button>
                        {item.threadId && onOpenThread && (
                          <Button variant="ghost" onClick={() => onOpenThread(item)}>
                            Open thread
                          </Button>
                        )}
                        <Button
                          variant="danger"
                          disabled={readOnly || pending !== undefined || deleting}
                          onClick={() => void requestDelete(item)}
                        >
                          {deleting ? "Deleting…" : "Delete"}
                        </Button>
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
