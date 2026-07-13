import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import Header from "../components/Header";
import TabBar from "../components/TabBar";
import type { DashboardRouteDefinition } from "./routes";

interface DashboardAppProps {
  routes: readonly DashboardRouteDefinition[];
}

export default function DashboardApp({ routes }: DashboardAppProps) {
  const defaultRoute = routes.find((route) => route.isDefault);
  if (!defaultRoute) {
    throw new Error("DashboardApp requires a default view route");
  }

  return (
    <BrowserRouter basename="/app">
      <Header />
      <TabBar
        tabs={routes.map((route) => ({
          id: route.viewId,
          label: route.label,
          to: `/${route.path}`,
        }))}
      />
      <Routes>
        <Route index element={<Navigate replace to={`/${defaultRoute.path}`} />} />
        {routes.map((route) => {
          const ViewComponent = route.component;
          return (
            <Route
              key={route.viewId}
              path={route.path}
              element={<ViewComponent />}
            />
          );
        })}
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
    </BrowserRouter>
  );
}
