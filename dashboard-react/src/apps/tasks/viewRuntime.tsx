import type { ViewSnapshot } from "../../dashboard/contributions/contracts";
import type {
  StandardViewRuntimeConfiguration,
  StandardViewRuntimeContext,
} from "../../dashboard/contributions/viewModules";
import { LocalStoragePersonalizationRepository } from "../../dashboard/personalization/repository";
import type { TasksViewModel } from "./contracts";
import { HttpTasksProvider } from "./providers/HttpTasksProvider";
import "./styles.css";

export const hostContractVersion = 1 as const;

export function createRuntime(
  context: StandardViewRuntimeContext,
): StandardViewRuntimeConfiguration {
  const provider = new HttpTasksProvider({
    location: context.location,
    navigate: (href) => window.location.assign(href),
  });
  return {
    provider,
    providerLabel: "Authoritative Tasks",
    personalizationRepository: new LocalStoragePersonalizationRepository(context.storage),
    renderChrome: (snapshot: ViewSnapshot) => {
      const model = snapshot.model as TasksViewModel | null;
      return (
        <header className="wb-tasks-chrome" aria-labelledby="wb-tasks-title">
          <div>
            <p className="wb-tasks-chrome__eyebrow">SQLite authority · Co-work knowledge</p>
            <h1 id="wb-tasks-title">Tasks</h1>
          </div>
          {model === null ? null : (
            <p className="wb-tasks-chrome__summary">
              <strong>{model.facets.counts.active ?? model.tasks.length}</strong> active
              <span aria-hidden="true"> · </span>
              <strong>{model.facets.counts.focused ?? 0}</strong> focused
              <span aria-hidden="true"> · </span>
              <strong>{model.facets.counts.inbox ?? 0}</strong> inbox
            </p>
          )}
        </header>
      );
    },
  };
}
