import { NavLink } from "react-router-dom";

export interface DashboardTab {
  readonly id: string;
  readonly label: string;
  readonly to: string;
}

export default function TabBar({ tabs }: { tabs: readonly DashboardTab[] }) {
  return (
    <nav className="tab-bar" aria-label="Dashboard views">
      {tabs.map((tab) => (
        <NavLink
          key={tab.id}
          to={tab.to}
          className={({ isActive }) => `tab-btn${isActive ? " active" : ""}`}
        >
          {tab.label}
        </NavLink>
      ))}
    </nav>
  );
}
