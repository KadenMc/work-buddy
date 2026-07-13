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

/** Journal-owned date/boundary/access chrome; widget composition remains host-owned. */
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
            ◫
          </div>
          <div>
            <div className="journal-view-chrome__title-row">
              <button
                type="button"
                className="journal-view-chrome__day-button"
                aria-label="Open previous Journal day"
                disabled={onNavigateDay === undefined}
                onClick={() => onNavigateDay?.("previous")}
              >
                ‹
              </button>
              <h1 id="journal-view-title">Journal</h1>
              <button
                type="button"
                className="journal-view-chrome__day-button"
                aria-label="Open next Journal day"
                disabled={onNavigateDay === undefined}
                onClick={() => onNavigateDay?.("next")}
              >
                ›
              </button>
            </div>
            <p className="journal-view-chrome__date">{formatDate(day)}</p>
            <p className="journal-view-chrome__metadata">
              <span>Day boundary {formatTime(day.dayBoundaryStart)}</span>
              {day.openedAt !== undefined ? (
                <span>Opened {formatTime(day.openedAt, day.timezone)}</span>
              ) : null}
            </p>
          </div>
        </div>

        <div className="journal-view-chrome__actions">
          {source.kind !== "live" ? (
            <span className="journal-view-chrome__badge" role="status">
              {source.label}
            </span>
          ) : null}
          {onReturnToToday !== undefined ? (
            <button
              type="button"
              className="journal-view-chrome__today-button"
              onClick={onReturnToToday}
            >
              Today
            </button>
          ) : null}
        </div>
      </div>

      {access.mode === "read_only" ? (
        <p className="journal-view-chrome__notice journal-view-chrome__notice--warning" role="status">
          <strong>Read only.</strong> {access.reason}
        </p>
      ) : null}

      {quality.freshness !== "current" ? (
        <p className="journal-view-chrome__notice" role="status">
          <strong>{quality.freshness === "offline" ? "Offline." : "Data may be stale."}</strong>{" "}
          {issueMessage}
        </p>
      ) : null}
    </header>
  );
}

export default JournalViewChrome;
