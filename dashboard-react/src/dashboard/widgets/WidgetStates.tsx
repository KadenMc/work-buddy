import { Button, InlineAlert, Spinner } from "../../ui";
import type { SnapshotStatus } from "../contributions/contracts";

export type WidgetHostStatus = SnapshotStatus | "loading" | "empty";

interface StateCopy {
  readonly title: string;
  readonly message: string;
}

const stateCopy: Record<WidgetHostStatus, StateCopy> = {
  loading: {
    title: "Loading widget",
    message: "Work Buddy is preparing this widget.",
  },
  empty: {
    title: "Nothing here yet",
    message: "This widget has no items to show.",
  },
  ready: { title: "Ready", message: "This widget is ready." },
  stale: {
    title: "May be out of date",
    message: "Showing the most recent available information while Work Buddy refreshes.",
  },
  offline: {
    title: "Offline",
    message: "Showing saved information. Reconnect to refresh this widget.",
  },
  unavailable: {
    title: "Temporarily unavailable",
    message: "This widget is not available right now.",
  },
  "permission-denied": {
    title: "Access needed",
    message: "This widget does not currently have permission to show its information.",
  },
  "read-only": {
    title: "Read-only",
    message: "You can review this widget, but changes are currently disabled.",
  },
  error: {
    title: "Widget could not load",
    message: "The rest of the view is still available. Try this widget again.",
  },
};

const retryable = new Set<WidgetHostStatus>([
  "offline",
  "unavailable",
  "error",
]);

export interface WidgetStateProps {
  readonly state: WidgetHostStatus;
  readonly message?: string;
  readonly onRetry?: () => void;
}

export function WidgetState({ state, message, onRetry }: WidgetStateProps) {
  const copy = stateCopy[state];
  const role =
    state === "error" || state === "unavailable" ? "alert" : "status";
  return (
    <div className="wb-widget-state" role={role}>
      {state === "loading" && <Spinner label={copy.title} />}
      <h3 className="wb-widget-state__title">{copy.title}</h3>
      <p>{message ?? copy.message}</p>
      {onRetry && retryable.has(state) && (
        <Button className="wb-widget-state__action" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

export function WidgetStatusBanner({
  state,
  message,
}: Pick<WidgetStateProps, "state" | "message">) {
  const copy = stateCopy[state];
  const tone = state === "stale" || state === "offline" ? "warning" : "info";
  return (
    <InlineAlert
      className="wb-widget-status-banner"
      tone={tone}
      role="status"
    >
      <strong>{copy.title}:</strong> {message ?? copy.message}
    </InlineAlert>
  );
}
