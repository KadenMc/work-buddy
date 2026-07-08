import { useEffect, useState } from "react";

// Same data source as the legacy header (core/event_bus.py): one
// EventSource on /api/events. "live" while the stream is open,
// "reconnecting" after an error (EventSource retries on its own with
// browser-managed backoff, so no manual reconnect here), "connecting"
// before the first open, "offline" if EventSource is unavailable.
export type LiveState = "connecting" | "live" | "reconnecting" | "offline";

export function useLiveStatus(): LiveState {
  const [state, setState] = useState<LiveState>("connecting");

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      setState("offline");
      return;
    }
    const es = new EventSource("/api/events");
    const onOpen = () => setState("live");
    const onError = () => setState("reconnecting");
    es.addEventListener("open", onOpen);
    es.addEventListener("error", onError);
    return () => es.close();
  }, []);

  return state;
}
