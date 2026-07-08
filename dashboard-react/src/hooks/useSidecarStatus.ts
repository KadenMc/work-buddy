import { useEffect, useState } from "react";

// Same data source and cadence as the legacy header (core/page.py
// refreshHeaderState): GET /api/state once on load and again whenever the
// browser tab returns to the foreground. Live changes between those
// moments are the SSE stream's job, not a poll loop's.
export type SidecarState = "unknown" | "running" | "stopped";

export interface SidecarStatus {
  state: SidecarState;
  readOnly: boolean;
}

export function useSidecarStatus(): SidecarStatus {
  const [status, setStatus] = useState<SidecarStatus>({
    state: "unknown",
    readOnly: false,
  });

  useEffect(() => {
    let cancelled = false;

    async function refresh() {
      try {
        const resp = await fetch("/api/state");
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: unknown = await resp.json();
        if (cancelled) return;
        const record = (data ?? {}) as Record<string, unknown>;
        setStatus({
          // /api/state reports "running" when the sidecar state file is
          // live and "unavailable" when it is missing; the legacy header
          // renders everything non-running as stopped.
          state: record.status === "running" ? "running" : "stopped",
          readOnly: Boolean(record.read_only),
        });
      } catch {
        // Endpoint unreachable (dashboard down, dev server without a
        // proxy target): degrade to unknown rather than claiming stopped.
        if (!cancelled) setStatus({ state: "unknown", readOnly: false });
      }
    }

    refresh();
    const onVisibility = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  return status;
}
