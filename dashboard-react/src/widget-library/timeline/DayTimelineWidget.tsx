import { useEffect, useState } from "react";
import { ArrowsClockwise } from "@phosphor-icons/react/ArrowsClockwise";

import type { WidgetRendererProps } from "../../dashboard/contributions/contracts";
import { Button, InlineAlert, SegmentedControl } from "../../ui";
import { createWidgetIntent } from "../shared";
import type {
  DayTimelineInput,
  DayTimelineIntent,
  DayTimelineItem,
  TimelineRenderMode,
} from "./contracts";
import { TemporalCanvas, TemporalList } from "./TemporalCanvas";

export default function DayTimelineWidget({
  input,
  emit,
  presentation,
}: WidgetRendererProps<DayTimelineInput, DayTimelineIntent>) {
  const [renderMode, setRenderMode] = useState(input.renderMode);
  const compact = presentation.sizeMode === "compact";
  const effectiveMode = compact ? "list" : renderMode;
  const readOnly = input.access?.mode === "read_only";

  useEffect(() => setRenderMode(input.renderMode), [input.renderMode]);

  const setMode = (next: TimelineRenderMode) => {
    setRenderMode(next);
    emit(
      createWidgetIntent(presentation, "wb.timeline.render-mode-changed", {
        render_mode: next,
      }) as DayTimelineIntent,
    );
  };
  const openItem = (item: DayTimelineItem) => {
    emit(
      createWidgetIntent(presentation, "wb.timeline.open-item", {
        item_id: item.itemId,
      }) as DayTimelineIntent,
    );
  };
  const requestReplan = () => {
    emit(
      createWidgetIntent(presentation, "wb.timeline.replan-requested", {
        day_id: input.day.dayId,
        preserve_before: input.day.now,
      }) as DayTimelineIntent,
    );
  };

  return (
    <div className="wb-day-timeline">
      <div className="wb-day-timeline__toolbar">
        {!compact && (
          <SegmentedControl
            label="Timeline display mode"
            value={renderMode}
            options={[
              { value: "timeline", label: "Timeline" },
              { value: "list", label: "List" },
            ]}
            onChange={setMode}
          />
        )}
        {presentation.sizeMode === "expanded" && (
          <Button size="small" disabled={readOnly} onClick={requestReplan}>
            <ArrowsClockwise aria-hidden="true" /> Request replan
          </Button>
        )}
      </div>
      {readOnly && <InlineAlert tone="warning">{input.access?.reason}</InlineAlert>}
      {input.items.length === 0 ? (
        <p className="wb-day-timeline__empty">No temporal items for this day.</p>
      ) : effectiveMode === "timeline" ? (
        <TemporalCanvas
          day={input.day}
          items={input.items}
          density={input.density}
          onOpenItem={openItem}
        />
      ) : (
        <TemporalList day={input.day} items={input.items} onOpenItem={openItem} />
      )}
      <p className="wb-visually-hidden">
        Every item includes textual kind, status, provenance, and mutability; color is
        supplementary.
      </p>
    </div>
  );
}
