import { useMemo, type ReactNode } from "react";

import type {
  WidgetDefinition,
  WidgetPresentationContext,
} from "../dashboard/contributions/contracts";
import {
  InMemoryWidgetDraftRepository,
  WidgetDraftRuntimeProvider,
  WidgetDraftScopeProvider,
} from "../dashboard/drafts";
import { InteractionSurfaceProvider } from "../dashboard/interactions";

export function DashboardTestRuntime({ children }: { readonly children: ReactNode }) {
  const repository = useMemo(() => new InMemoryWidgetDraftRepository(), []);
  return (
    <InteractionSurfaceProvider>
      <WidgetDraftRuntimeProvider
        repository={repository}
        profileId="test-profile"
        workspaceId="test-workspace"
      >
        {children}
      </WidgetDraftRuntimeProvider>
    </InteractionSurfaceProvider>
  );
}

export function WidgetDraftTestScope({
  children,
  definition,
  presentation,
  input,
}: {
  readonly children: ReactNode;
  readonly definition: WidgetDefinition;
  readonly presentation: WidgetPresentationContext;
  readonly input: unknown;
}) {
  return (
    <DashboardTestRuntime>
      <WidgetDraftScopeProvider
        definition={definition}
        viewId={presentation.viewId}
        instanceId={presentation.instanceId}
        input={input}
      >
        {children}
      </WidgetDraftScopeProvider>
    </DashboardTestRuntime>
  );
}
