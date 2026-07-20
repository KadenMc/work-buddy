import { lazy, Suspense } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";

import Header from "../components/Header";
import TabBar from "../components/TabBar";
import { CustomizeModeProvider } from "../dashboard/customize";
import { HelpModeProvider } from "../dashboard/help";
import { SettingsRegistryProvider } from "../settings";
import type { DashboardRouteDefinition } from "./routes";

const SettingsPage = lazy(() =>
  import("../settings/SettingsPage").then((module) => ({
    default: module.SettingsPage,
  })),
);

const WidgetLab = import.meta.env.DEV
  ? lazy(() => import("../dev/widget-lab/WidgetLab"))
  : null;
const CalendarSpike = import.meta.env.DEV
  ? lazy(() => import("../dev/calendar-spike/CalendarSpike"))
  : null;

interface DashboardAppProps {
  routes: readonly DashboardRouteDefinition[];
}

function LegacyAccessibilitySettingsRedirect() {
  const location = useLocation();
  return (
    <Navigate
      replace
      to="/settings/system/accessibility"
      state={location.state}
    />
  );
}

function LegacyJournalViewSettingsRedirect() {
  const location = useLocation();
  return (
    <Navigate
      replace
      to={`/settings/apps/journal${location.search}${location.hash}`}
      state={location.state}
    />
  );
}

function DashboardShell({ routes }: DashboardAppProps) {
  const location = useLocation();
  const defaultRoute = routes.find((route) => route.isDefault);
  if (!defaultRoute) {
    throw new Error("DashboardApp requires a default view route");
  }
  const isSettings = location.pathname.startsWith("/settings");
  const defaultViewPath = `/${defaultRoute.path}`;

  return (
    <>
      <Header defaultViewPath={defaultViewPath} />
      {!isSettings && (
        <TabBar
          tabs={routes.map((route) => ({
            id: route.viewId,
            label: route.label,
            to: `/${route.path}`,
          }))}
        />
      )}
      <Routes>
        <Route index element={<Navigate replace to={`/${defaultRoute.path}`} />} />
        {routes.map((route) => {
          const ViewComponent = route.component;
          return (
            <Route
              key={route.viewId}
              path={route.path}
              element={
                <Suspense
                  fallback={
                    <main className="tab-panel" aria-busy="true">
                      <div className="empty-state">Loading view…</div>
                    </main>
                  }
                >
                  <ViewComponent />
                </Suspense>
              }
            />
          );
        })}
        {WidgetLab && (
          <Route
            path="__widget-lab"
            element={
              <Suspense
                fallback={
                  <main className="tab-panel" aria-busy="true">
                    <div className="empty-state">Loading Widget Lab…</div>
                  </main>
                }
              >
                <WidgetLab />
              </Suspense>
            }
          />
        )}
        {CalendarSpike && (
          <Route
            path="__calendar-spike"
            element={
              <Suspense
                fallback={
                  <main className="tab-panel" aria-busy="true">
                    <div className="empty-state">Loading Calendar spike…</div>
                  </main>
                }
              >
                <CalendarSpike />
              </Suspense>
            }
          />
        )}
        <Route
          path="settings/accessibility"
          element={<LegacyAccessibilitySettingsRedirect />}
        />
        <Route
          path="settings/views/journal"
          element={<LegacyJournalViewSettingsRedirect />}
        />
        <Route
          path="settings"
          element={<Navigate replace to="/settings/system/accessibility" />}
        />
        <Route
          path="settings/*"
          element={
            <Suspense
              fallback={
                <main className="tab-panel" aria-busy="true">
                  <div className="empty-state">Loading Settings…</div>
                </main>
              }
            >
              <SettingsPage defaultViewPath={defaultViewPath} />
            </Suspense>
          }
        />
        <Route
          path="*"
          element={
            <main className="tab-panel">
              <div className="empty-state">
                <div className="empty-state-title">View not found</div>
                <div className="empty-state-hint">
                  Choose an available view from the dashboard navigation.
                </div>
              </div>
            </main>
          }
        />
      </Routes>
    </>
  );
}

export default function DashboardApp(props: DashboardAppProps) {
  return (
    <BrowserRouter basename="/app">
      <SettingsRegistryProvider>
        <HelpModeProvider>
          <CustomizeModeProvider>
            <DashboardShell {...props} />
          </CustomizeModeProvider>
        </HelpModeProvider>
      </SettingsRegistryProvider>
    </BrowserRouter>
  );
}
