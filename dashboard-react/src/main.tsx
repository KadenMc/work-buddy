import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/geist/index.css";
import "@fontsource-variable/geist-mono/index.css";
import App from "./App";
import { DashboardAnnouncer } from "./dashboard/accessibility/DashboardAnnouncer";
import { DashboardEventProvider } from "./dashboard/events/DashboardEventProvider";
import { DensityProvider } from "./theme/DensityProvider";
import { ThemeProvider } from "./theme/ThemeProvider";
import "./theme.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <DensityProvider>
        <DashboardEventProvider>
          <DashboardAnnouncer>
            <App />
          </DashboardAnnouncer>
        </DashboardEventProvider>
      </DensityProvider>
    </ThemeProvider>
  </StrictMode>,
);
