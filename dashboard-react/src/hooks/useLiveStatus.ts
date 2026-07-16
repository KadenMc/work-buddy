import {
  useDashboardEvents,
  type DashboardConnectionState,
} from "../dashboard/events/DashboardEventProvider";

export type LiveState = DashboardConnectionState;

/** Header compatibility hook; transport ownership lives at the dashboard root. */
export function useLiveStatus(): LiveState {
  return useDashboardEvents().connectionState;
}
