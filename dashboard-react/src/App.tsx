import DashboardApp from "./app/DashboardApp";
import { dashboardRoutes } from "./app/routes";

export default function App() {
  return <DashboardApp routes={dashboardRoutes} />;
}
