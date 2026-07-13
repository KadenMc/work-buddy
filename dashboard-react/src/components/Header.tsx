import { useClock } from "../hooks/useClock";
import { useLiveStatus } from "../hooks/useLiveStatus";
import { useSidecarStatus } from "../hooks/useSidecarStatus";
import { ThemeSchemeControl } from "../theme/ThemeSchemeControl";

type DotKind = "healthy" | "unhealthy" | "stopped";

function StatusDot({ kind }: { kind: DotKind }) {
  return <span className={`status-dot ${kind}`} />;
}

// Mirrors the legacy header's #sidecar-status rendering
// (work_buddy/dashboard/frontend/scripts/core/page.py refreshHeaderState):
// /api/state with status === "running" is a healthy dot and "sidecar
// running", anything else is "sidecar stopped". A fetch failure is the
// degraded "sidecar unknown" state, which the legacy header left as
// "loading...".
function SidecarIndicator() {
  const { state, readOnly } = useSidecarStatus();
  const dot: DotKind = state === "running" ? "healthy" : "stopped";
  const label = state === "unknown" ? "sidecar unknown" : `sidecar ${state}`;
  return (
    <span title="Sidecar service status (/api/state)">
      <StatusDot kind={dot} /> {label}
      {readOnly && <span className="read-only-tag"> (read-only)</span>}
    </span>
  );
}

// Presents the root DashboardEventProvider's connection state. Header is a
// consumer only: it never creates a second EventSource.
function LiveIndicator() {
  const live = useLiveStatus();
  const dot: DotKind =
    live === "live" ? "healthy" : live === "reconnecting" ? "unhealthy" : "stopped";
  const label = live === "connecting" ? "live" : live;
  return (
    <span className={`bus-status ${live}`} title="Real-time event stream (/api/events)">
      <StatusDot kind={dot} /> {label}
    </span>
  );
}

function Clock() {
  const time = useClock();
  return <span className="clock">{time}</span>;
}

export default function Header() {
  return (
    <header className="header">
      <h1>
        <span>work-buddy</span> dashboard
      </h1>
      <div className="header-meta">
        <ThemeSchemeControl />
        <SidecarIndicator />
        <LiveIndicator />
        <Clock />
      </div>
    </header>
  );
}
