import "./theme/layers.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/geist/index.css";
import "@fontsource-variable/geist-mono/index.css";
import App from "./App";
import { DashboardAnnouncer } from "./dashboard/accessibility/DashboardAnnouncer";
import {
  createBrowserWidgetDraftRepository,
  WidgetDraftRuntimeProvider,
} from "./dashboard/drafts";
import { DashboardEventProvider } from "./dashboard/events/DashboardEventProvider";
import { InteractionSurfaceProvider } from "./dashboard/interactions";
import { DashboardTemporalContextProvider } from "./dashboard/temporal/DashboardTemporalContext";
import { DensityProvider } from "./theme/DensityProvider";
import { ThemeProvider } from "./theme/ThemeProvider";
import { TypographyScaleProvider } from "./theme/TypographyScaleProvider";
import {
  currentLocalIdentity,
  hasLocalIdentityBootstrap,
  initializeLocalIdentity,
  refreshLocalIdentity,
} from "./security/localIdentity";
import "./theme.css";

const widgetDraftRepository = createBrowserWidgetDraftRepository();

// Consume a launcher-delivered bootstrap before a feature can issue a
// human-authority mutation. Ordinary local editing does not depend on this
// stronger boundary; focus recovery lets another trusted app tab restore it.
void initializeLocalIdentity();
window.addEventListener("hashchange", () => {
  if (hasLocalIdentityBootstrap()) void refreshLocalIdentity();
});
const recoverFocusedLocalIdentity = (): void => {
  if (!currentLocalIdentity().authenticated) void refreshLocalIdentity();
};
window.addEventListener("focus", recoverFocusedLocalIdentity);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") recoverFocusedLocalIdentity();
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <TypographyScaleProvider>
        <DensityProvider>
          <DashboardTemporalContextProvider>
            <DashboardEventProvider>
              <DashboardAnnouncer>
                <InteractionSurfaceProvider>
                  <WidgetDraftRuntimeProvider repository={widgetDraftRepository}>
                    <App />
                  </WidgetDraftRuntimeProvider>
                </InteractionSurfaceProvider>
              </DashboardAnnouncer>
            </DashboardEventProvider>
          </DashboardTemporalContextProvider>
        </DensityProvider>
      </TypographyScaleProvider>
    </ThemeProvider>
  </StrictMode>,
);
