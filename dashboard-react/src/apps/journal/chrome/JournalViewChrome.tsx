import { CalendarDots } from "@phosphor-icons/react/CalendarDots";
import { CaretLeft } from "@phosphor-icons/react/CaretLeft";
import { CaretRight } from "@phosphor-icons/react/CaretRight";
import { Clock } from "@phosphor-icons/react/Clock";
import { Database } from "@phosphor-icons/react/Database";
import { SunHorizon } from "@phosphor-icons/react/SunHorizon";

import { Button, IconButton, InlineAlert } from "../../../ui";
import type {
  JournalAccess,
  JournalDataQuality,
  JournalDayBinding,
  JournalDemoSource,
} from "../contracts";

export interface JournalViewChromeProps {
  readonly day: JournalDayBinding;
  readonly access: JournalAccess;
  readonly quality: JournalDataQuality;
  readonly source: JournalDemoSource;
  readonly onNavigateDay?: (direction: "previous" | "next") => void;
  readonly onReturnToToday?: () => void;
}

function formatDate(day: JournalDayBinding): string {
  try {
    return new Intl.DateTimeFormat("en-US", {
      weekday: "long",
      month: "long",
      day: "numeric",
      year: "numeric",
      timeZone: day.timezone,
    }).format(new Date(day.windowStart));
  } catch {
    return day.localDate;
  }
}

function formatTime(value: string, timezone?: string): string {
  const date = value.includes("T") ? new Date(value) : new Date(`1970-01-01T${value}:00`);
  if (!Number.isFinite(date.getTime())) return value;
  try {
    return new Intl.DateTimeFormat("en-US", {
      hour: "numeric",
      minute: "2-digit",
      ...(timezone === undefined ? {} : { timeZone: timezone }),
    }).format(date);
  } catch {
    return value;
  }
}

/** Journal owns meaning and actions; Dashboard Core owns the visual primitives. */
export function JournalViewChrome({
  day,
  access,
  quality,
  source,
  onNavigateDay,
  onReturnToToday,
}: JournalViewChromeProps) {
  const issueMessage = quality.issues.map((issue) => issue.message).join(" ");

  return (
    <header className="journal-view-chrome" aria-labelledby="journal-view-title">
      <div className="journal-view-chrome__main">
        <div className="journal-view-chrome__identity">
          <div className="journal-view-chrome__mark" aria-hidden="true">
            <CalendarDots weight="duotone" />
          </div>
          <div className="journal-view-chrome__copy">
            <div className="journal-view-chrome__title-row">
              {onNavigateDay ? (
                <IconButton
                  label="Open previous Journal day"
                  icon={<CaretLeft weight="bold" />}
                  variant="ghost"
                  size="small"
                  onClick={() => onNavigateDay("previous")}
                />
              ) : null}
              <h1 id="journal-view-title">Journal</h1>
              {onNavigateDay ? (
                <IconButton
                  label="Open next Journal day"
                  icon={<CaretRight weight="bold" />}
                  variant="ghost"
                  size="small"
                  onClick={() => onNavigateDay("next")}
                />
              ) : null}
            </div>
            <p className="journal-view-chrome__date">{formatDate(day)}</p>
            <div className="journal-view-chrome__metadata">
              <span>
                <SunHorizon weight="duotone" aria-hidden="true" />
                Day starts {formatTime(day.dayBoundaryStart)}
              </span>
              {day.openedAt !== undefined ? (
                <span>
                  <Clock weight="duotone" aria-hidden="true" />
                  Opened {formatTime(day.openedAt, day.timezone)}
                </span>
              ) : null}
            </div>
          </div>
        </div>

        <div className="journal-view-chrome__actions">
          {source.kind !== "live" ? (
            <span className="journal-view-chrome__source" role="status">
              <Database weight="duotone" aria-hidden="true" />
              {source.label}
            </span>
          ) : null}
          {onReturnToToday !== undefined ? (
            <Button size="small" variant="ghost" onClick={onReturnToToday}>
              Today
            </Button>
          ) : null}
        </div>
      </div>

      {access.mode === "read_only" ? (
        <InlineAlert className="journal-view-chrome__notice" tone="warning" role="status">
          <strong>Read only.</strong> {access.reason}
        </InlineAlert>
      ) : null}

      {quality.freshness !== "current" ? (
        <InlineAlert className="journal-view-chrome__notice" role="status">
          <strong>{quality.freshness === "offline" ? "Offline." : "Data may be stale."}</strong>{" "}
          {issueMessage}
        </InlineAlert>
      ) : null}
    </header>
  );
}

export default JournalViewChrome;
