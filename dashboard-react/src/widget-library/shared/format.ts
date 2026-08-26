export function formatTime(value: string, timezone?: string): string {
  const instant = new Date(value);
  if (!Number.isFinite(instant.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    timeZone: timezone,
  }).format(instant);
}

export function formatTimeRange(
  start: string,
  end: string | undefined,
  timezone?: string,
): string {
  return end === undefined
    ? formatTime(start, timezone)
    : `${formatTime(start, timezone)}–${formatTime(end, timezone)}`;
}

/** Display only: never replace a persisted actor reference or assert "You". */
export function formatActorLabel(value: unknown): string {
  let kind: unknown = typeof value === "string" ? value : null;
  if (typeof value === "string") {
    try {
      const actor: unknown = JSON.parse(value);
      kind = actor !== null && typeof actor === "object" && !Array.isArray(actor)
        && "schema" in actor && actor.schema === "wb.actor-ref/v1" && "kind" in actor
        ? actor.kind
        : null;
    } catch {
      // Older document events may carry a plain actor-kind label.
    }
  }
  switch (kind) {
    case "human":
      return "Human";
    case "agent_run":
      return "AI run";
    case "service":
    case "system":
      return "Work Buddy";
    default:
      return "Recorded actor";
  }
}
