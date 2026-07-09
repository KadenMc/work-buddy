"""Read-only sidecar status for the tray (no Qt imports).

Reads the same pid + state files ``wbuddy status`` reads and classifies them
with the same shared helpers (``lifecycle.daemon_health`` /
``lifecycle.dispatch_busy``), so the tray can never disagree with the CLI.
Also home to the pure UI-model helpers (tooltip text, menu enabled-ness) so
they are unit-testable without Qt.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TrayStatus:
    health: str            # down | booting | up | wedged
    busy: dict | None      # {"phase", "job", "busy_for_s"} or None
    services_healthy: int
    services_total: int
    pid: int | None
    services: dict = field(default_factory=dict)  # name -> status str (for the panel list)


def read_status() -> TrayStatus:
    from work_buddy.cli import lifecycle
    from work_buddy.sidecar import pid as _pid
    from work_buddy.sidecar import state as _state

    pid = _pid.check_existing_daemon()
    st = _state.load_state()
    health = lifecycle.daemon_health(pid, st)
    busy = lifecycle.dispatch_busy(st) if health == "up" else None
    services: dict[str, str] = {}
    if health == "up" and st is not None and st.services:
        services = {name: svc.status for name, svc in sorted(st.services.items())}
    healthy = sum(1 for s in services.values() if s == "healthy")
    return TrayStatus(
        health=health,
        busy=busy,
        services_healthy=healthy,
        services_total=len(services),
        pid=pid,
        services=services,
    )


def icon_key(status: TrayStatus) -> str:
    """Which icon variant to show: the four health states, busy as an overlay."""
    if status.health == "up" and status.busy:
        return "busy"
    return status.health


def tooltip(status: TrayStatus) -> str:
    if status.health == "up":
        base = f"work-buddy: running ({status.services_healthy}/{status.services_total} services healthy)"
        if status.busy:
            job = status.busy.get("job")
            what = f"job '{job}'" if job else status.busy.get("phase", "dispatch")
            base += f", busy on {what}"
        return base
    if status.health == "booting":
        return "work-buddy: starting up..."
    if status.health == "wedged":
        return "work-buddy: wedged (not responding), use Restart"
    return "work-buddy: stopped"


def menu_enabled(health: str, action_pending: bool) -> dict[str, bool]:
    """Enabled-ness of the lifecycle menu items for a given health state.

    Start also covers takeover of a wedged daemon (mirroring ``wbuddy start``);
    everything is disabled while a menu action is still running.
    """
    if action_pending:
        return {"start": False, "stop": False, "restart": False}
    return {
        "start": health in ("down", "wedged"),
        "stop": health != "down",
        "restart": health != "down",
    }
