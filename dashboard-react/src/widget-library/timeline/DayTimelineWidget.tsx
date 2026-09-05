import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ArrowsClockwise } from "@phosphor-icons/react/ArrowsClockwise";

import type {
  IntentResult,
  WidgetRendererProps,
} from "../../dashboard/contributions/contracts";
import { useWidgetDraft } from "../../dashboard/drafts";
import { useInteractionSurfaces } from "../../dashboard/interactions";
import { Button, InlineAlert, SegmentedControl, TextAreaField } from "../../ui";
import { createCorrelationId, createWidgetIntent } from "../shared";
import {
  journalInstantAtLocalTime,
  journalLocalTimeForInstant,
} from "../shared/journalDayTime";
import {
  CALENDAR_RECORD_DELETE_ACTION_ID,
  CALENDAR_RECORD_EDIT_ACTION_ID,
} from "./calendar-surface/actions";
import type {
  CalendarSurfaceIntent,
  CalendarSurfaceIntentResult,
} from "./calendar-surface/contracts";
import { CalendarSurface } from "./calendar-surface/CalendarSurface";
import {
  timelineItemAcceptsContentEdits,
  toCalendarSurfaceModel,
} from "./calendar-surface/fromDayTimeline";
import type {
  DayTimelineInput,
  DayTimelineIntent,
  DayTimelineItem,
  TimelineRenderMode,
  TimelineTemporalPlacement,
} from "./contracts";
import "./styles.css";

/** One record open for correction, held as a recoverable host draft. */
interface RecordEditSession {
  readonly itemId: string;
  readonly expectedVersion: number;
  readonly text: string;
  readonly localTime: string;
  readonly openedLocalTime: string;
}

/** A dispatched correction the provider has accepted but not yet reloaded. */
interface PendingRecordEdit {
  readonly expectedVersion: number;
  readonly text: string;
  readonly at?: string;
}

/** A record narrowed to everything a correction needs from it. */
interface CorrectableRecord {
  readonly item: DayTimelineItem;
  readonly version: number;
  readonly text: string;
  readonly occurredAt: string;
}

const occurrenceOf = (item: DayTimelineItem): string =>
  item.shape === "point" ? item.at : item.startAt;

/**
 * Mirror the display split the provider applies, so a dispatched correction
 * reads the way it will read once the reload lands.
 */
const withPendingEdit = (
  item: DayTimelineItem,
  pending: PendingRecordEdit,
): DayTimelineItem => {
  const lines = pending.text.split(/\r?\n/u);
  const title = lines.find((line) => line.trim().length > 0)?.trim() ?? item.title;
  const detail = lines.slice(1).join("\n").trim();
  const placement: TimelineTemporalPlacement =
    item.shape === "point"
      ? { shape: "point", at: pending.at ?? item.at }
      : { shape: "span", startAt: item.startAt, endAt: item.endAt };
  return {
    ...placement,
    itemId: item.itemId,
    kind: item.kind,
    title,
    ...(detail.length === 0 ? {} : { detail }),
    status: item.status,
    mutability: item.mutability,
    precision: item.precision,
    provenance: item.provenance,
    text: pending.text,
    ...(item.version === undefined ? {} : { version: item.version }),
    ...(item.authorityKind === undefined ? {} : { authorityKind: item.authorityKind }),
    ...(item.navigation === undefined ? {} : { navigation: item.navigation }),
  };
};

const asSurfaceResult = (
  result: IntentResult,
  fallbackRevision: string,
): CalendarSurfaceIntentResult => ({
  status: result.status,
  revision: typeof result.revision === "string" ? result.revision : fallbackRevision,
  ...(result.message === undefined ? {} : { message: result.message }),
});

export default function DayTimelineWidget({
  input,
  emit,
  presentation,
}: WidgetRendererProps<DayTimelineInput, DayTimelineIntent>) {
  const { confirm } = useInteractionSurfaces();
  const editDraft = useWidgetDraft<RecordEditSession | null>("record-edit", null, {
    isPristine: (value) => value === null,
  });
  const edit = editDraft.value;
  const setEdit = editDraft.setValue;
  const timeFieldId = useId();
  const [renderMode, setRenderMode] = useState(input.renderMode);
  const [announcement, setAnnouncement] = useState("");
  const [saveError, setSaveError] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const [pendingEdits, setPendingEdits] = useState<
    Readonly<Record<string, PendingRecordEdit>>
  >({});
  const [pendingDeletes, setPendingDeletes] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  // A failure already rendered as a visible alert is not repeated in the live region.
  const alertedFailure = useRef<string | undefined>(undefined);
  const compact = presentation.sizeMode === "compact";
  const density = compact ? "compact" : input.density;
  const readOnly = input.access?.mode === "read_only";
  const items = useMemo(
    () =>
      input.items
        .filter((item) => !pendingDeletes.has(item.itemId))
        .map((item) => {
          const pending = pendingEdits[item.itemId];
          return pending === undefined ? item : withPendingEdit(item, pending);
        }),
    [input.items, pendingDeletes, pendingEdits],
  );
  const calendarModel = useMemo(
    () => toCalendarSurfaceModel({ ...input, renderMode, items }),
    [input, items, renderMode],
  );
  const editingItem =
    edit === null
      ? undefined
      : input.items.find((item) => item.itemId === edit.itemId);
  const conflict =
    edit !== null &&
    editingItem?.version !== undefined &&
    editingItem.version !== edit.expectedVersion;

  useEffect(() => setRenderMode(input.renderMode), [input.renderMode]);

  // A dispatched correction stays painted until the provider's reload carries a
  // revision past the one it was written against.
  useEffect(() => {
    setPendingEdits((current) => {
      const next = Object.fromEntries(
        Object.entries(current).filter(([itemId, pending]) => {
          const item = input.items.find((candidate) => candidate.itemId === itemId);
          return (
            item === undefined ||
            item.version === undefined ||
            item.version <= pending.expectedVersion
          );
        }),
      );
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
  }, [input.items]);

  useEffect(() => {
    const presentIds = new Set(input.items.map((item) => item.itemId));
    setPendingDeletes((current) => {
      const next = new Set([...current].filter((itemId) => presentIds.has(itemId)));
      return next.size === current.size ? current : next;
    });
  }, [input.items]);

  /** A record whose dispatched change has not settled would only conflict again. */
  const settling = (itemId: string): boolean =>
    pendingEdits[itemId] !== undefined || pendingDeletes.has(itemId);

  const correctableRecord = (itemId: string): CorrectableRecord | undefined => {
    const item = input.items.find((candidate) => candidate.itemId === itemId);
    if (item === undefined || readOnly || !timelineItemAcceptsContentEdits(item)) {
      return undefined;
    }
    if (item.text === undefined || item.version === undefined) return undefined;
    return {
      item,
      version: item.version,
      text: item.text,
      occurredAt: occurrenceOf(item),
    };
  };

  const setMode = (next: TimelineRenderMode) => {
    setRenderMode(next);
    emit(
      createWidgetIntent(presentation, "wb.timeline.render-mode-changed", {
        render_mode: next,
      }) as DayTimelineIntent,
    );
  };
  const requestReplan = () => {
    void emit(
      createWidgetIntent(presentation, "wb.timeline.replan-requested", {
        day_id: input.day.dayId,
        preserve_before: input.day.now,
      }) as DayTimelineIntent,
    );
  };

  const beginEdit = (itemId: string): CalendarSurfaceIntentResult => {
    // A draft still loading would overwrite the session it is handed.
    if (!editDraft.ready) {
      return {
        status: "unavailable",
        revision: input.revision,
        message: "The editor is still restoring an earlier draft. Try again in a moment.",
      };
    }
    if (settling(itemId)) {
      return {
        status: "unavailable",
        revision: input.revision,
        message: "This record is still saving. Try again once it settles.",
      };
    }
    const record = correctableRecord(itemId);
    if (record === undefined) {
      return {
        status: "unavailable",
        revision: input.revision,
        message: "This record is not editable here.",
      };
    }
    const localTime = journalLocalTimeForInstant(record.occurredAt, input.day.timezone);
    setSaveError(undefined);
    setActionError(undefined);
    setEdit({
      itemId,
      expectedVersion: record.version,
      text: record.text,
      localTime,
      openedLocalTime: localTime,
    });
    return { status: "accepted", revision: input.revision };
  };

  const requestDelete = async (
    itemId: string,
  ): Promise<CalendarSurfaceIntentResult> => {
    if (settling(itemId)) {
      return {
        status: "unavailable",
        revision: input.revision,
        message: "This record is still saving. Try again once it settles.",
      };
    }
    const record = correctableRecord(itemId);
    if (record === undefined) {
      return {
        status: "unavailable",
        revision: input.revision,
        message: "This record is not deletable here.",
      };
    }
    setActionError(undefined);
    const accepted = await confirm({
      title: "Delete this record?",
      description:
        "It leaves the day's timeline. Work Buddy keeps the history behind it so downstream context stays accurate.",
      confirmLabel: "Delete record",
      cancelLabel: "Keep record",
      tone: "danger",
    });
    if (!accepted) {
      return {
        status: "rejected",
        revision: input.revision,
        message: "Deletion cancelled.",
      };
    }
    const result = await emit(
      createWidgetIntent(
        presentation,
        "wb.timeline.item-delete-requested",
        { item_id: itemId, expected_version: record.version },
        { clientMutationId: createCorrelationId("timeline-delete") },
      ) as DayTimelineIntent,
    );
    if (result.status !== "accepted") {
      const message = result.message ?? `The record deletion was ${result.status}.`;
      alertedFailure.current = message;
      setActionError(message);
      return asSurfaceResult(result, input.revision);
    }
    setPendingDeletes((current) => new Set(current).add(itemId));
    return asSurfaceResult(result, input.revision);
  };

  const saveEdit = async () => {
    if (edit === null || conflict) return;
    const record = correctableRecord(edit.itemId);
    if (record === undefined) {
      setSaveError("This record is no longer editable here.");
      return;
    }
    let statedAt: string | undefined;
    if (edit.localTime !== edit.openedLocalTime) {
      try {
        statedAt = journalInstantAtLocalTime(input.day, edit.localTime);
      } catch (error) {
        setSaveError(
          error instanceof Error ? error.message : "Enter a valid Journal time.",
        );
        return;
      }
    }
    const submittedRevision = editDraft.revision;
    try {
      await editDraft.flush();
    } catch {
      return;
    }
    const result = await emit(
      createWidgetIntent(
        presentation,
        "wb.timeline.item-edit-requested",
        {
          item_id: edit.itemId,
          expected_version: edit.expectedVersion,
          text: edit.text,
          ...(statedAt === undefined ? {} : { stated_at: statedAt }),
        },
        { clientMutationId: createCorrelationId("timeline-edit") },
      ) as DayTimelineIntent,
    );
    if (result.status !== "accepted") {
      setSaveError(result.message ?? `The record update was ${result.status}.`);
      return;
    }
    setPendingEdits((current) => ({
      ...current,
      [edit.itemId]: {
        expectedVersion: edit.expectedVersion,
        text: edit.text,
        ...(statedAt === undefined ? {} : { at: statedAt }),
      },
    }));
    setSaveError(undefined);
    await editDraft.clear({ ifRevision: submittedRevision });
  };

  const handleCalendarIntent = async (
    intent: CalendarSurfaceIntent,
  ): Promise<CalendarSurfaceIntentResult> => {
    if (intent.type === "calendar.range-requested") {
      return { status: "accepted", revision: input.revision };
    }
    if (intent.type === "calendar.item-open-requested") {
      const result = await emit(
        createWidgetIntent(presentation, "wb.timeline.open-item", {
          item_id: intent.itemId,
        }) as DayTimelineIntent,
      );
      return asSurfaceResult(result, input.revision);
    }
    if (intent.type === "calendar.item-action-requested") {
      if (intent.actionId === CALENDAR_RECORD_EDIT_ACTION_ID) {
        return beginEdit(intent.itemId);
      }
      if (intent.actionId === CALENDAR_RECORD_DELETE_ACTION_ID) {
        return requestDelete(intent.itemId);
      }
      const result = await emit(
        createWidgetIntent(
          presentation,
          "wb.timeline.item-action-requested",
          {
            item_id: intent.itemId,
            action_id: intent.actionId,
            expected_revision: intent.expectedRevision,
          },
          {
            intentId: intent.requestId,
            clientMutationId: intent.requestId,
          },
        ) as DayTimelineIntent,
      );
      return asSurfaceResult(result, input.revision);
    }
    return {
      status: "unavailable",
      revision: input.revision,
      message: "This Journal timeline action is not available yet.",
    };
  };

  return (
    <div className="wb-day-timeline">
      <div className="wb-day-timeline__toolbar">
        <SegmentedControl
          label="Timeline display mode"
          value={renderMode}
          options={[
            { value: "timeline", label: "Timeline" },
            { value: "list", label: "List" },
          ]}
          onChange={setMode}
        />
        {presentation.sizeMode === "expanded" && (
          <Button size="small" disabled={readOnly} onClick={requestReplan}>
            <ArrowsClockwise aria-hidden="true" /> Request replan
          </Button>
        )}
      </div>
      {readOnly && input.accessNotice !== "view" ? (
        <InlineAlert tone="warning">{input.access?.reason}</InlineAlert>
      ) : null}
      {actionError ? (
        <InlineAlert tone="danger" role="alert">
          {actionError}
        </InlineAlert>
      ) : null}
      {edit !== null && editDraft.ready ? (
        <section className="wb-day-timeline__editor" aria-label="Edit record">
          {conflict ? (
            <InlineAlert tone="warning">
              This record changed while you were editing. Cancel and reopen it before
              saving.
            </InlineAlert>
          ) : null}
          {editDraft.error ? (
            <InlineAlert tone="danger">{editDraft.error}</InlineAlert>
          ) : null}
          {saveError ? <InlineAlert tone="danger">{saveError}</InlineAlert> : null}
          <TextAreaField
            label="Record text"
            value={edit.text}
            rows={compact ? 3 : 5}
            onChange={(text) => setEdit({ ...edit, text })}
          />
          <div className="wb-day-timeline__editor-time">
            <label htmlFor={timeFieldId}>Record time</label>
            <input
              id={timeFieldId}
              type="time"
              value={edit.localTime}
              onChange={(event) =>
                setEdit({ ...edit, localTime: event.target.value })
              }
            />
            <small>{input.day.timezone}</small>
          </div>
          <div className="wb-day-timeline__editor-actions">
            <Button onClick={() => void editDraft.clear()}>Cancel</Button>
            <Button
              variant="primary"
              disabled={conflict}
              onClick={() => void saveEdit()}
            >
              Save
            </Button>
          </div>
        </section>
      ) : null}
      {items.length === 0 ? (
        <p className="wb-day-timeline__empty">No temporal items for this day.</p>
      ) : (
        <CalendarSurface
          model={calendarModel}
          density={density}
          onIntent={handleCalendarIntent}
          onAnnouncement={(message) => {
            if (message === alertedFailure.current) {
              alertedFailure.current = undefined;
              return;
            }
            setAnnouncement(message);
          }}
        />
      )}
      <p className="wb-visually-hidden" role="status">
        {announcement}
      </p>
      <p className="wb-visually-hidden">
        Every item includes textual kind, status, provenance, and mutability; color is
        supplementary.
      </p>
    </div>
  );
}
