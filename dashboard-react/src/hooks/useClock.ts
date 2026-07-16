import { useEffect, useState } from "react";

import type { DashboardTemporalContext } from "../dashboard/temporal/DashboardTemporalContext";

// 12-hour h:mm with AM/PM, zero-padded, e.g. "03:18 PM". The legacy
// header clock (core/page.py updateClock) uses the same 2-digit
// hour/minute options with a 10 s tick; the locale is pinned here so the
// format does not drift on 24-hour system locales.
export function formatClock(d: Date, timezone: string): string {
  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
    timeZone: timezone,
  }).format(d);
}

/**
 * Advance from a server-observed instant while formatting exclusively in the
 * host-owned Work Buddy zone. An unavailable context never falls back to the
 * browser timezone, because that would make dashboard day semantics ambiguous.
 */
export function useClock(context?: DashboardTemporalContext): string | undefined {
  const [now, setNow] = useState<string | undefined>(() =>
    context === undefined ? undefined : formatClock(new Date(context.now), context.timezone),
  );
  useEffect(() => {
    if (context === undefined) {
      setNow(undefined);
      return undefined;
    }
    const serverBaseline = Date.parse(context.now);
    const clientBaseline = Date.now();
    const update = () =>
      setNow(
        formatClock(
          new Date(serverBaseline + (Date.now() - clientBaseline)),
          context.timezone,
        ),
      );
    update();
    const id = setInterval(update, 10_000);
    return () => clearInterval(id);
  }, [context]);
  return now;
}
