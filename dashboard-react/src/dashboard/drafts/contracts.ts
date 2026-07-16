import type {
  AppId,
  JsonSchemaReference,
  JsonValue,
  ViewId,
  WidgetInstanceId,
  WidgetTypeId,
} from "../contributions/contracts";

export interface WidgetDraftIdentity {
  readonly profileId: string;
  readonly workspaceId: string;
  readonly appId: AppId;
  readonly viewId: ViewId;
  readonly instanceId: WidgetInstanceId;
  readonly widgetTypeId: WidgetTypeId;
  readonly draftName: string;
  readonly scopeKey: string;
}

export interface WidgetDraftEnvelope extends WidgetDraftIdentity {
  readonly envelopeVersion: 1;
  readonly storageKey: string;
  readonly draftSchema: JsonSchemaReference;
  readonly revision: number;
  readonly value: JsonValue;
  readonly updatedAt: string;
  readonly expiresAt?: string;
}

export interface SaveWidgetDraftRequest extends WidgetDraftIdentity {
  readonly draftSchema: JsonSchemaReference;
  readonly value: JsonValue;
  readonly expectedRevision?: number;
  readonly retentionDays?: number;
}

export interface WidgetDraftRepository {
  load(identity: WidgetDraftIdentity): Promise<WidgetDraftEnvelope | undefined>;
  save(request: SaveWidgetDraftRequest): Promise<WidgetDraftEnvelope>;
  delete(identity: WidgetDraftIdentity, expectedRevision?: number): Promise<void>;
  subscribe?(listener: (storageKey: string) => void): () => void;
}

export class WidgetDraftConflictError extends Error {
  constructor(readonly storageKey: string) {
    super("This draft changed in another dashboard surface.");
    this.name = "WidgetDraftConflictError";
  }
}

export const widgetDraftStorageKey = (identity: WidgetDraftIdentity): string =>
  [
    identity.profileId,
    identity.workspaceId,
    identity.appId,
    identity.viewId,
    identity.instanceId,
    identity.widgetTypeId,
    identity.draftName,
    identity.scopeKey,
  ]
    .map((part) => encodeURIComponent(part))
    .join("|");
