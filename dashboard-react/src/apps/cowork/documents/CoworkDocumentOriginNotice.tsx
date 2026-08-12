import { useEffect, useMemo, useState } from "react";

type UnknownRecord = Record<string, unknown>;

interface ChangeSource {
  readonly sourceRef: string;
  readonly sourceRole: string;
  readonly originatingSurface: string;
  readonly providerId: string | null;
  readonly lifecycleState: string;
  readonly copyRelation: "exact_copy" | "source_backed_change";
}

interface ChangeBinding {
  readonly domainNamespace: string;
  readonly domainKind: string;
  readonly domainEntityId: string;
  readonly contentAuthority: string;
}

export interface CoworkDocumentChangeInspection {
  readonly changeId: string;
  readonly operationKind: string;
  readonly committedAt: string;
  readonly actors: Readonly<Record<string, unknown>>;
  readonly assurance: Readonly<Record<string, unknown>>;
  readonly source: ChangeSource | null;
  readonly binding: ChangeBinding | null;
}

const record = (value: unknown): UnknownRecord | null =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;

const text = (value: unknown): string | null =>
  typeof value === "string" && value.length > 0 ? value : null;

const nullableText = (value: unknown): string | null | undefined =>
  value === null ? null : text(value) ?? undefined;

export const parseCoworkDocumentChangeInspection = (
  value: unknown,
): CoworkDocumentChangeInspection | null => {
  const payload = record(value);
  if (
    payload === null ||
    payload.schema !== "wb.cowork-document-change-inspection/v1"
  ) {
    return null;
  }
  const changeId = text(payload.change_id);
  const operationKind = text(payload.operation_kind);
  const committedAt = text(payload.committed_at);
  const actors = record(payload.actors);
  const assurance = record(payload.assurance);
  if (
    changeId === null ||
    operationKind === null ||
    committedAt === null ||
    actors === null ||
    assurance === null
  ) {
    return null;
  }
  const rawSource = payload.source === null ? null : record(payload.source);
  let source: ChangeSource | null = null;
  if (rawSource !== null) {
    const sourceRef = text(rawSource.source_ref);
    const sourceRole = text(rawSource.source_role);
    const originatingSurface = text(rawSource.originating_surface);
    const lifecycleState = text(rawSource.lifecycle_state);
    const providerId = nullableText(rawSource.provider_id);
    const copyRelation = rawSource.copy_relation;
    if (
      sourceRef === null ||
      sourceRole === null ||
      originatingSurface === null ||
      lifecycleState === null ||
      providerId === undefined ||
      (copyRelation !== "exact_copy" && copyRelation !== "source_backed_change")
    ) {
      return null;
    }
    source = {
      sourceRef,
      sourceRole,
      originatingSurface,
      providerId,
      lifecycleState,
      copyRelation,
    };
  } else if (payload.source !== null) {
    return null;
  }
  const rawBinding = payload.binding === null ? null : record(payload.binding);
  let binding: ChangeBinding | null = null;
  if (rawBinding !== null) {
    const domainNamespace = text(rawBinding.domain_namespace);
    const domainKind = text(rawBinding.domain_kind);
    const domainEntityId = text(rawBinding.domain_entity_id);
    const contentAuthority = text(rawBinding.content_authority);
    if (
      domainNamespace === null ||
      domainKind === null ||
      domainEntityId === null ||
      contentAuthority === null
    ) {
      return null;
    }
    binding = {
      domainNamespace,
      domainKind,
      domainEntityId,
      contentAuthority,
    };
  } else if (payload.binding !== null) {
    return null;
  }
  return {
    changeId,
    operationKind,
    committedAt,
    actors,
    assurance,
    source,
    binding,
  };
};

const actorKind = (value: unknown): string | null => {
  if (typeof value !== "string") return null;
  try {
    return text(record(JSON.parse(value))?.kind);
  } catch {
    return null;
  }
};

const actorLabel = (value: unknown): string => {
  switch (actorKind(value)) {
    case "human":
      return "Human";
    case "agent_run":
      return "AI run";
    case "service":
    case "system":
      return "Work Buddy";
    default:
      return "Recorded actor";
  }
};

const sourceTitle = (inspection: CoworkDocumentChangeInspection): string => {
  if (
    inspection.binding?.domainNamespace === "journal" &&
    inspection.binding.domainKind === "running_note"
  ) {
    return "From a Running Note";
  }
  return inspection.source === null ? "Recorded document change" : "From a recorded source";
};

interface CoworkDocumentOriginNoticeProps {
  readonly storeId: string;
  readonly documentId: string;
  readonly changeId: string;
  readonly fetcher?: typeof fetch;
}

export function CoworkDocumentOriginNotice({
  storeId,
  documentId,
  changeId,
  fetcher = fetch,
}: CoworkDocumentOriginNoticeProps) {
  const [inspection, setInspection] = useState<CoworkDocumentChangeInspection | null>(
    null,
  );
  const [failed, setFailed] = useState(false);
  const endpoint = useMemo(
    () =>
      `/api/truth/doc/${encodeURIComponent(documentId)}/changes/${encodeURIComponent(changeId)}` +
      `?store_id=${encodeURIComponent(storeId)}`,
    [changeId, documentId, storeId],
  );

  useEffect(() => {
    const controller = new AbortController();
    setInspection(null);
    setFailed(false);
    void fetcher(endpoint, { credentials: "same-origin", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("change_inspection_unavailable");
        const parsed = parseCoworkDocumentChangeInspection(await response.json());
        if (parsed === null) throw new Error("change_inspection_invalid");
        setInspection(parsed);
      })
      .catch((error: unknown) => {
        if (!(error instanceof Error && error.name === "AbortError")) {
          setFailed(true);
        }
      });
    return () => controller.abort();
  }, [endpoint, fetcher]);

  if (failed) {
    return (
      <p className="wb-cowork-origin-notice wb-cowork-origin-notice--unavailable" role="status">
        Source details are temporarily unavailable.
      </p>
    );
  }
  if (inspection === null) {
    return (
      <p className="wb-cowork-origin-notice" role="status">
        Loading source details…
      </p>
    );
  }
  const selectedBy = inspection.actors.selected_by ?? inspection.actors.input_by;
  const appliedBy = inspection.actors.applied_by;
  return (
    <details className="wb-cowork-origin-notice">
      <summary title="View the recorded source, actors, and assurances for this document change.">
        <strong>{sourceTitle(inspection)}</strong>
        <span>
          {inspection.source?.copyRelation === "exact_copy"
            ? "Exact source copy"
            : "Source-backed change"}
        </span>
      </summary>
      <dl>
        <div>
          <dt>Source</dt>
          <dd>
            {inspection.source?.originatingSurface ?? "Unavailable"}
            {inspection.source?.providerId === null || inspection.source === null
              ? null
              : ` · ${inspection.source.providerId}`}
          </dd>
        </div>
        <div>
          <dt>Selected by</dt>
          <dd>{actorLabel(selectedBy)}</dd>
        </div>
        <div>
          <dt>Applied by</dt>
          <dd>{actorLabel(appliedBy)}</dd>
        </div>
        <div>
          <dt>Persistence</dt>
          <dd>
            {inspection.assurance.persistence === "persistence_verified"
              ? "Verified"
              : "Recorded without verification"}
          </dd>
        </div>
        <div>
          <dt>Source state</dt>
          <dd>{inspection.source?.lifecycleState ?? "Unavailable"}</dd>
        </div>
      </dl>
    </details>
  );
}
