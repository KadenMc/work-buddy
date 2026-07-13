import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { DashboardAnnouncer } from "./dashboard/accessibility/DashboardAnnouncer";
import { DashboardEventProvider } from "./dashboard/events/DashboardEventProvider";
import { ThemeProvider } from "./theme/ThemeProvider";
import "./theme.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <DashboardEventProvider>
        <DashboardAnnouncer>
          <App />
        </DashboardAnnouncer>
      </DashboardEventProvider>
    </ThemeProvider>
  </StrictMode>,
);
