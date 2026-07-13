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
