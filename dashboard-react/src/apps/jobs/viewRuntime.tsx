import type { StandardViewRuntimeContext } from "../../dashboard/contributions/viewModules";
import { LocalStoragePersonalizationRepository } from "../../dashboard/personalization/repository";
import { HttpJobsProvider } from "./HttpJobsProvider";
import type { JobAuthoringInput } from "./contracts";
import { InlineAlert } from "../../ui";
import "./styles.css";

export const hostContractVersion = 1 as const;
export function createRuntime(context: StandardViewRuntimeContext) {
  return {
    provider: new HttpJobsProvider(globalThis.fetch.bind(globalThis), context.location), providerLabel: "Jobs",
    personalizationRepository: new LocalStoragePersonalizationRepository(context.storage),
    renderChrome: (snapshot: import("../../dashboard/contributions/contracts").ViewSnapshot) => {
      const input = snapshot.model as JobAuthoringInput | null;
      return <header className="wb-jobs-chrome"><h1>Jobs</h1><a href="/#tab=jobs">Manage existing jobs</a>{input?.access.mode === "read_only" ? <InlineAlert tone="warning">{input.access.reason}</InlineAlert> : null}</header>;
    },
  };
}
