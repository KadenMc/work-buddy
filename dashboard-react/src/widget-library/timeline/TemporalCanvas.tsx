import type { CSSProperties } from "react";

import { ProvenanceBadge, StatusBadge, formatTimeRange } from "../shared";
import type {
  DayTimelineItem,
  TimelineDayWindow,
  TimelineDensity,
} from "./contracts";
import "./styles.css";

interface PositionedItem {
  readonly item: DayTimelineItem;
  readonly start: number;
  readonly end: number;
  readonly lane: number;
}

const TIMELINE_HEIGHT_PX: Record<TimelineDensity, number> = {
  comfortable: 720,
  compact: 560,
};

/** Keep lane allocation consistent with the card's CSS minimum height. */
const MINIMUM_CARD_HEIGHT_PX = 42;

interface TimelinePositionStyle extends CSSProperties {
  readonly "--wb-timeline-top": string;
  readonly "--wb-timeline-height": string;
  readonly "--wb-timeline-left": string;
  readonly "--wb-timeline-width": string;
}

const itemTimes = (item: DayTimelineItem): readonly [number, number] => {
  const start = Date.parse(item.shape === "point" ? item.at : item.startAt);
  const end = Date.parse(item.shape === "point" ? item.at : item.endAt);
  return [start, end];
};

function positionItems(
  items: readonly DayTimelineItem[],
  windowDuration: number,
  density: TimelineDensity,
): {
  readonly items: readonly PositionedItem[];
  readonly lanes: number;
} {
  const laneEnds: number[] = [];
  const minimumVisibleDuration =
    (windowDuration * MINIMUM_CARD_HEIGHT_PX) / TIMELINE_HEIGHT_PX[density];
  const positioned = [...items]
    .map((item) => ({ item, times: itemTimes(item) }))
    .filter(({ times }) => times.every(Number.isFinite))
    .sort((left, right) => left.times[0] - right.times[0])
    .map(({ item, times }) => {
      const [start, end] = times;
      let lane = laneEnds.findIndex((laneEnd) => laneEnd <= start);
      if (lane < 0) lane = laneEnds.length;
      laneEnds[lane] = Math.max(end, start + minimumVisibleDuration);
      return { item, start, end, lane };
    });
  return { items: positioned, lanes: Math.max(1, laneEnds.length) };
}

const mutabilityLabel: Record<DayTimelineItem["mutability"], string> = {
  editable: "editable",
  fixed: "fixed commitment",
  past_protected: "past — protected",
};

function TemporalItemContent({
  item,
  day,
}: {
  readonly item: DayTimelineItem;
  readonly day: TimelineDayWindow;
}) {
  const start = item.shape === "point" ? item.at : item.startAt;
  const end = item.shape === "span" ? item.endAt : undefined;
  return (
    <>
      <span className="wb-temporal-item__time">
        {formatTimeRange(start, end, day.timezone)}
      </span>
      <strong className="wb-temporal-item__title">{item.title}</strong>
      {item.detail && <span className="wb-temporal-item__detail">{item.detail}</span>}
      <span className="wb-library-meta-row">
        <StatusBadge label={item.kind} />
        <StatusBadge
          label={item.status}
          tone={item.status === "cancelled" ? "danger" : item.status === "completed" ? "success" : "info"}
        />
        <StatusBadge
          label={mutabilityLabel[item.mutability]}
          tone={item.mutability === "fixed" ? "warning" : "neutral"}
        />
        <ProvenanceBadge provenance={item.provenance} />
      </span>
    </>
  );
}

export interface TemporalCanvasProps {
  readonly day: TimelineDayWindow;
  readonly items: readonly DayTimelineItem[];
  readonly density: TimelineDensity;
  onOpenItem(item: DayTimelineItem): void;
}

export function TemporalCanvas({
  day,
  items,
  density,
  onOpenItem,
}: TemporalCanvasProps) {
  const windowStart = Date.parse(day.windowStart);
  const windowEnd = Date.parse(day.windowEnd);
  const duration = Math.max(1, windowEnd - windowStart);
  const now = Date.parse(day.now);
  const positioned = positionItems(items, duration, density);
  const ticks = Array.from({ length: 13 }, (_, index) => {
    const instant = windowStart + (duration * index) / 12;
    return { instant, top: (index / 12) * 100 };
  });

  return (
    <div className={`wb-temporal-canvas wb-temporal-canvas--${density}`}>
      <div className="wb-temporal-canvas__axis" aria-hidden="true">
        {ticks.map((tick) => (
          <span key={tick.instant} style={{ top: `${tick.top}%` }}>
            {formatTimeRange(new Date(tick.instant).toISOString(), undefined, day.timezone)}
          </span>
        ))}
      </div>
      <div className="wb-temporal-canvas__track">
        {ticks.map((tick) => (
          <span
            key={tick.instant}
            className="wb-temporal-canvas__grid-line"
            style={{ top: `${tick.top}%` }}
            aria-hidden="true"
          />
        ))}
        {Number.isFinite(now) && now >= windowStart && now <= windowEnd && (
          <span
            className="wb-temporal-canvas__now"
            style={{ top: `${((now - windowStart) / duration) * 100}%` }}
            aria-hidden="true"
          />
        )}
        {positioned.items
          .filter(({ start, end }) => start <= windowEnd && end >= windowStart)
          .map(({ item, start, end, lane }) => {
          const top = ((start - windowStart) / duration) * 100;
          const height = Math.max(
            item.shape === "point" ? 0.8 : 1.3,
            ((Math.max(end, start + 60_000) - start) / duration) * 100,
          );
          const width = 100 / positioned.lanes;
          const style: TimelinePositionStyle = {
            "--wb-timeline-top": `${Math.max(0, Math.min(100, top))}%`,
            "--wb-timeline-height": `${Math.min(100 - Math.max(0, top), height)}%`,
            "--wb-timeline-left": `${lane * width}%`,
            "--wb-timeline-width": `${width}%`,
          };
            return (
              <button
                key={item.itemId}
                type="button"
                className={`wb-temporal-item wb-temporal-item--${item.kind}`}
                style={style}
                onClick={() => onOpenItem(item)}
              >
                <TemporalItemContent item={item} day={day} />
              </button>
            );
          })}
      </div>
    </div>
  );
}

export function TemporalList({
  day,
  items,
  onOpenItem,
}: Omit<TemporalCanvasProps, "density">) {
  const ordered = [...items].sort((left, right) => itemTimes(left)[0] - itemTimes(right)[0]);
  return (
    <ol className="wb-temporal-list" aria-label="Day timeline items">
      {ordered.map((item) => (
        <li key={item.itemId}>
          <button type="button" onClick={() => onOpenItem(item)}>
            <TemporalItemContent item={item} day={day} />
          </button>
        </li>
      ))}
    </ol>
  );
}
