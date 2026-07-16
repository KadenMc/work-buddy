import { CalendarBlank } from "@phosphor-icons/react/CalendarBlank";
import { CheckCircle } from "@phosphor-icons/react/CheckCircle";
import { Circle } from "@phosphor-icons/react/Circle";
import { ClockCountdown } from "@phosphor-icons/react/ClockCountdown";
import type { CSSProperties } from "react";

import { Pressable } from "../../ui";
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

const MINIMUM_CARD_HEIGHT_PX = 56;
const HOUR_MS = 60 * 60 * 1000;

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

function visibleWindow(day: TimelineDayWindow, items: readonly DayTimelineItem[]) {
  const dayStart = Date.parse(day.windowStart);
  const dayEnd = Date.parse(day.windowEnd);
  const now = Date.parse(day.now);
  const itemInstants = items.flatMap((item) => itemTimes(item)).filter(Number.isFinite);
  const anchors = [
    ...itemInstants,
    ...(Number.isFinite(now) && now >= dayStart && now <= dayEnd ? [now] : []),
  ];
  if (anchors.length === 0) return { start: dayStart, end: dayEnd };
  const earliest = Math.min(...anchors);
  const latest = Math.max(...anchors);
  const paddedStart = Math.floor((earliest - HOUR_MS) / HOUR_MS) * HOUR_MS;
  const paddedEnd = Math.ceil((latest + HOUR_MS) / HOUR_MS) * HOUR_MS;
  const minimumEnd = paddedStart + 6 * HOUR_MS;
  return {
    start: Math.max(dayStart, Math.min(paddedStart, dayEnd - 6 * HOUR_MS)),
    end: Math.min(dayEnd, Math.max(paddedEnd, minimumEnd)),
  };
}

function positionItems(
  items: readonly DayTimelineItem[],
  windowDuration: number,
  canvasHeight: number,
): { readonly items: readonly PositionedItem[]; readonly lanes: number } {
  const laneEnds: number[] = [];
  const minimumVisibleDuration = (windowDuration * MINIMUM_CARD_HEIGHT_PX) / canvasHeight;
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

const kindIcon = (item: DayTimelineItem) => {
  if (item.status === "completed") return <CheckCircle weight="fill" />;
  if (item.kind === "calendar") return <CalendarBlank weight="duotone" />;
  if (item.kind === "plan") return <ClockCountdown weight="duotone" />;
  return <Circle weight="fill" />;
};

function TemporalItemContent({ item, day }: { readonly item: DayTimelineItem; readonly day: TimelineDayWindow }) {
  const start = item.shape === "point" ? item.at : item.startAt;
  const end = item.shape === "span" ? item.endAt : undefined;
  return (
    <>
      <span className="wb-temporal-item__icon" aria-hidden="true">{kindIcon(item)}</span>
      <span className="wb-temporal-item__copy">
        <span className="wb-temporal-item__topline">
          <span className="wb-temporal-item__time">{formatTimeRange(start, end, day.timezone)}</span>
          <StatusBadge label={item.kind} />
        </span>
        <strong className="wb-temporal-item__title">{item.title}</strong>
        {item.detail ? <span className="wb-temporal-item__detail">{item.detail}</span> : null}
        <span className="wb-library-meta-row wb-temporal-item__meta">
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

export function TemporalCanvas({ day, items, density, onOpenItem }: TemporalCanvasProps) {
  const window = visibleWindow(day, items);
  const duration = Math.max(1, window.end - window.start);
  const now = Date.parse(day.now);
  const canvasHeight = density === "compact" ? 420 : 520;
  const positioned = positionItems(items, duration, canvasHeight);
  const tickCount = Math.max(2, Math.min(9, Math.round(duration / HOUR_MS) + 1));
  const ticks = Array.from({ length: tickCount }, (_, index) => {
    const instant = window.start + (duration * index) / (tickCount - 1);
    return { instant, top: (index / (tickCount - 1)) * 100 };
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
          <span key={tick.instant} className="wb-temporal-canvas__grid-line" style={{ top: `${tick.top}%` }} aria-hidden="true" />
        ))}
        {Number.isFinite(now) && now >= window.start && now <= window.end ? (
          <span className="wb-temporal-canvas__now" style={{ top: `${((now - window.start) / duration) * 100}%` }} aria-hidden="true">
            <span>Now</span>
          </span>
        ) : null}
        {positioned.items
          .filter(({ start, end }) => start <= window.end && end >= window.start)
          .map(({ item, start, end, lane }) => {
            const top = ((start - window.start) / duration) * 100;
            const height = Math.max(1.3, ((Math.max(end, start + 60_000) - start) / duration) * 100);
            const width = 100 / positioned.lanes;
            const boundedTop = Math.max(0, Math.min(100, top));
            const durationMinutes = Math.max(0, (end - start) / 60_000);
            const style: TimelinePositionStyle = {
              "--wb-timeline-top": `${boundedTop}%`,
              "--wb-timeline-height": `${Math.min(100 - boundedTop, height)}%`,
              "--wb-timeline-left": `${lane * width}%`,
              "--wb-timeline-width": `${width}%`,
            };
            return (
              <Pressable
                key={item.itemId}
                className={`wb-temporal-item wb-temporal-item--${item.kind}${
                  durationMinutes < 60 ? " wb-temporal-item--condensed" : ""
                }`}
                style={style}
                onClick={() => onOpenItem(item)}
              >
                <TemporalItemContent item={item} day={day} />
              </Pressable>
            );
          })}
      </div>
    </div>
  );
}

export function TemporalList({ day, items, onOpenItem }: Omit<TemporalCanvasProps, "density">) {
  const ordered = [...items].sort((left, right) => itemTimes(left)[0] - itemTimes(right)[0]);
  return (
    <ol className="wb-temporal-list" aria-label="Day timeline items">
      {ordered.map((item) => (
        <li key={item.itemId}>
          <Pressable onClick={() => onOpenItem(item)}>
            <TemporalItemContent item={item} day={day} />
          </Pressable>
        </li>
      ))}
    </ol>
  );
}
