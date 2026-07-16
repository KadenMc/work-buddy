import type {
  JsonValue,
  WidgetIntent,
  WidgetPresentationContext,
} from "../../dashboard/contributions/contracts";

let fallbackSequence = 0;

export function createCorrelationId(prefix: string): string {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  if (randomUuid !== undefined) {
    return `${prefix}:${randomUuid}`;
  }
  fallbackSequence += 1;
  return `${prefix}:local-${fallbackSequence}`;
}

export function createWidgetIntent<Payload extends JsonValue>(
  presentation: Pick<WidgetPresentationContext, "instanceId" | "viewId">,
  intentType: string,
  payload: Payload,
  options?: {
    readonly intentId?: string;
    readonly clientMutationId?: string;
  },
): WidgetIntent<Payload> {
  return {
    intent_type: intentType,
    schema_version: 1,
    intent_id: options?.intentId ?? createCorrelationId("intent"),
    view_id: presentation.viewId,
    instance_id: presentation.instanceId,
    client_mutation_id: options?.clientMutationId,
    payload,
  };
}
