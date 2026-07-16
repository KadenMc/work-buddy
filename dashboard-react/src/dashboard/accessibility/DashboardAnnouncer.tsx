import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";

import { VisuallyHidden } from "../../ui";

export type AnnouncementPoliteness = "polite" | "assertive";

export interface DashboardAnnouncementRuntime {
  announce(message: string, politeness?: AnnouncementPoliteness): void;
}

const AnnouncementContext = createContext<DashboardAnnouncementRuntime | null>(null);

interface Announcement {
  readonly id: number;
  readonly message: string;
}

export function DashboardAnnouncer({ children }: { readonly children: ReactNode }) {
  const [polite, setPolite] = useState<Announcement>({ id: 0, message: "" });
  const [assertive, setAssertive] = useState<Announcement>({
    id: 0,
    message: "",
  });

  const announce = useCallback(
    (message: string, politeness: AnnouncementPoliteness = "polite") => {
      const update = (current: Announcement): Announcement => ({
        id: current.id + 1,
        message,
      });
      if (politeness === "assertive") {
        setAssertive(update);
      } else {
        setPolite(update);
      }
    },
    [],
  );

  const runtime = useMemo(() => ({ announce }), [announce]);

  return (
    <AnnouncementContext.Provider value={runtime}>
      {children}
      <VisuallyHidden
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        <span key={polite.id}>{polite.message}</span>
      </VisuallyHidden>
      <VisuallyHidden
        role="alert"
        aria-live="assertive"
        aria-atomic="true"
      >
        <span key={assertive.id}>{assertive.message}</span>
      </VisuallyHidden>
    </AnnouncementContext.Provider>
  );
}

export function useDashboardAnnouncer(): DashboardAnnouncementRuntime {
  const runtime = useContext(AnnouncementContext);
  if (runtime === null) {
    throw new Error("useDashboardAnnouncer must be used within DashboardAnnouncer");
  }
  return runtime;
}
