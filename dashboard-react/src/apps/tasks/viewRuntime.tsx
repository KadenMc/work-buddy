import type { ViewSnapshot } from "../../dashboard/contributions/contracts";
import type {
  StandardViewRuntimeConfiguration,
  StandardViewRuntimeContext,
} from "../../dashboard/contributions/viewModules";
import { LocalStoragePersonalizationRepository } from "../../dashboard/personalization/repository";
import type { TasksViewModel } from "./contracts";
import { HttpTasksProvider } from "./providers/HttpTasksProvider";
import { InlineAlert } from "../../ui";
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
    providerLabel: "Tasks",
    personalizationRepository: new LocalStoragePersonalizationRepository(context.storage),
    renderChrome: (snapshot: ViewSnapshot) => {
      const model = snapshot.model as TasksViewModel | null;
      return (
        <div>
          <header className="wb-tasks-chrome" aria-labelledby="wb-tasks-title">
            <div>
              <p className="wb-tasks-chrome__eyebrow">Tasks · Co-work knowledge</p>
              <h1 id="wb-tasks-title">Tasks</h1>
            </div>
            {model === null ? null : (
              <p className="wb-tasks-chrome__summary">
                <strong>{model.total ?? model.tasks.length}</strong> matching tasks
              </p>
            )}
          </header>
          {model?.access.mode === "read_only" ? (
            <InlineAlert tone="warning" role="status">
              {model.access.reason ?? "Task editing is temporarily unavailable."}
            </InlineAlert>
          ) : null}
        </div>
      );
    },
  };
}
