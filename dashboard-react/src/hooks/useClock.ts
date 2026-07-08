import { useEffect, useState } from "react";

// 12-hour h:mm with AM/PM, zero-padded, e.g. "03:18 PM". The legacy
// header clock (core/page.py updateClock) uses the same 2-digit
// hour/minute options with a 10 s tick; the locale is pinned here so the
// format does not drift on 24-hour system locales.
function formatClock(d: Date): string {
  return d.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  });
}

export function useClock(): string {
  const [now, setNow] = useState(() => formatClock(new Date()));
  useEffect(() => {
    const id = setInterval(() => setNow(formatClock(new Date())), 10_000);
    return () => clearInterval(id);
  }, []);
  return now;
}
