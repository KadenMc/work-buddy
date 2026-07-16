import { Sparkle } from "@phosphor-icons/react/Sparkle";

import { useDashboardTemporalContext } from "../dashboard/temporal/DashboardTemporalContext";
import { useClock } from "../hooks/useClock";
import { useLiveStatus } from "../hooks/useLiveStatus";
import { useSidecarStatus } from "../hooks/useSidecarStatus";
import { SettingsLauncher } from "../settings/SettingsNavigation";
import { AppearanceControl } from "../theme/AppearanceControl";

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
    <span className="header-status" title="Sidecar service status (/api/state)">
      <StatusDot kind={dot} />
      <span>{label}</span>
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
    <span className={`header-status bus-status ${live}`} title="Real-time event stream (/api/events)">
      <StatusDot kind={dot} />
      <span>{label}</span>
    </span>
  );
}

function Clock() {
  const temporal = useDashboardTemporalContext();
  const time = useClock(temporal.context);
  const title =
    temporal.status === "ready"
      ? `Work Buddy time · ${temporal.context.timezone}`
      : "Work Buddy timezone unavailable";
  return (
    <span className="clock" title={title} aria-label={title}>
      {time ?? "--:--"}
    </span>
  );
}

export default function Header({
  defaultViewPath = "/journal",
}: {
  readonly defaultViewPath?: string;
}) {
  return (
    <header className="header">
      <div className="header__brand">
        <span className="header__brand-mark" aria-hidden="true">
          <Sparkle weight="duotone" />
        </span>
        <h1>
          <span>work-buddy</span>
          <span className="header__descriptor">dashboard</span>
        </h1>
      </div>
      <div className="header-meta">
        <AppearanceControl />
        <div className="header-statuses">
          <SidecarIndicator />
          <LiveIndicator />
        </div>
        <Clock />
        <SettingsLauncher defaultViewPath={defaultViewPath} />
      </div>
    </header>
  );
}
