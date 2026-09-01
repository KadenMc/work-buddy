import { useEffect, useMemo, useState, type ReactNode } from "react";

import type {
  IntentResult,
  JsonValue,
  WidgetIntent,
  WidgetPresentationContext,
  WidgetRendererProps,
} from "../../dashboard/contributions/contracts";
import { Button, InlineAlert } from "../../ui";
import {
  useDocumentSession,
  type BoundDocumentRef,
} from "../cowork/session/DocumentSession";
import { DocumentWorkspacePanel } from "../cowork/surface/DocumentWorkspacePanel";
import { createCorrelationId, createWidgetIntent } from "../../widget-library/shared";
import type {
  JournalEffectiveFieldInput,
  JournalDocumentModuleCurrent,
  JournalFieldReference,
  JournalFieldValue,
  JournalFieldValuePutIntent,
  JournalGenericModuleInput,
  JournalItemAction,
  JournalNativeItemInput,
  JournalPromptInteraction,
} from "./contracts";

const isReference = (value: unknown): value is JournalFieldReference =>
  typeof value === "object" && value !== null
  && typeof (value as { kind?: unknown }).kind === "string"
  && typeof (value as { id?: unknown }).id === "string";

const valueText = (value: JournalFieldValue): string => {
  if (value === null) return "Not recorded";
  if (Array.isArray(value)) {
    if (value.length === 0) return "None";
    if (value.every((item) => typeof item === "string")) return value.join(", ");
    if (value.every(isReference)) {
      return value.map((item) => `${item.kind}: ${item.id}`).join(", ");
    }
  }
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
};

const editableText = (field: JournalEffectiveFieldInput): string => {
  if (field.value === null) return "";
  if (field.valueKind === "instant" && typeof field.value === "string") {
    const parsed = new Date(field.value);
    if (!Number.isNaN(parsed.valueOf())) return parsed.toISOString().slice(0, 16);
  }
  if (Array.isArray(field.value)) return JSON.stringify(field.value);
  return String(field.value);
};

const typedValue = (
  field: JournalEffectiveFieldInput,
  raw: string,
): JournalFieldValue => {
  if (field.valueKind === "number" || field.valueKind === "scale") {
    const value = Number(raw);
    if (raw.trim().length === 0 || !Number.isFinite(value)) {
      throw new Error("Enter a valid number.");
    }
    return value;
  }
  if (field.valueKind === "duration") {
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 0) {
      throw new Error("Enter a whole number of seconds.");
    }
    return value;
  }
  if (field.valueKind === "boolean") {
    if (raw !== "true" && raw !== "false") throw new Error("Choose Yes or No.");
    return raw === "true";
  }
  if (field.valueKind === "multi_select") {
    const value = JSON.parse(raw || "[]") as unknown;
    if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
      throw new Error("Choose valid options.");
    }
    return value;
  }
  if (field.valueKind === "reference") {
    const value = JSON.parse(raw || "[]") as unknown;
    if (!Array.isArray(value) || !value.every(isReference)) {
      throw new Error("References must be a JSON list of kind and id objects.");
    }
    return value;
  }
  if (field.valueKind === "instant") {
    const parsed = new Date(raw);
    if (raw.length === 0 || Number.isNaN(parsed.valueOf())) {
      throw new Error("Choose a valid date and time.");
    }
    return parsed.toISOString();
  }
  if (raw.length === 0 && field.required) throw new Error("This field is required.");
  return raw;
};

function FieldControl({
  field,
  raw,
  disabled,
  onChange,
}: {
  readonly field: JournalEffectiveFieldInput;
  readonly raw: string;
  readonly disabled: boolean;
  readonly onChange: (value: string) => void;
}) {
  if (field.valueKind === "long_text" || field.valueKind === "reference") {
    return (
      <textarea
        aria-label={field.label}
        value={raw}
        disabled={disabled}
        rows={field.valueKind === "reference" ? 3 : 4}
        placeholder={field.valueKind === "reference" ? '[{"kind":"project","id":"example"}]' : undefined}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }
  if (field.valueKind === "boolean") {
    return (
      <select aria-label={field.label} value={raw} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
        <option value="">Choose…</option>
        <option value="true">Yes</option>
        <option value="false">No</option>
      </select>
    );
  }
  if (field.valueKind === "single_select") {
    return (
      <select aria-label={field.label} value={raw} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
        <option value="">Choose…</option>
        {(field.options ?? []).map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    );
  }
  if (field.valueKind === "multi_select") {
    let selected: readonly string[] = [];
    try {
      const value = JSON.parse(raw || "[]") as unknown;
      if (Array.isArray(value) && value.every((item) => typeof item === "string")) selected = value;
    } catch {
      selected = [];
    }
    return (
      <fieldset className="journal-generic-module__options" disabled={disabled}>
        <legend>Options</legend>
        {(field.options ?? []).map((option) => (
          <label key={option.value}>
            <input
              type="checkbox"
              checked={selected.includes(option.value)}
              onChange={(event) => onChange(JSON.stringify(
                event.target.checked
                  ? [...selected, option.value]
                  : selected.filter((value) => value !== option.value),
              ))}
            />
            {option.label}
          </label>
        ))}
      </fieldset>
    );
  }
  const type = field.valueKind === "number" || field.valueKind === "scale"
    || field.valueKind === "duration"
    ? "number"
    : field.valueKind === "date"
      ? "date"
      : field.valueKind === "local_time"
        ? "time"
        : field.valueKind === "instant"
          ? "datetime-local"
          : "text";
  return (
    <input
      aria-label={field.label}
      type={type}
      value={raw}
      disabled={disabled}
      {...(field.minimum === undefined ? {} : { min: field.minimum })}
      {...(field.maximum === undefined ? {} : { max: field.maximum })}
      {...(field.valueKind === "duration" ? { step: 1 } : {})}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

function FieldValue({
  field,
  input,
  emit,
  presentation,
}: {
  readonly field: JournalEffectiveFieldInput;
  readonly input: JournalGenericModuleInput;
  readonly emit: (intent: WidgetIntent) => Promise<IntentResult>;
  readonly presentation: WidgetPresentationContext;
}) {
  const [editing, setEditing] = useState(false);
  const [raw, setRaw] = useState(() => editableText(field));
  const [disposition, setDisposition] = useState<"" | "missing" | "skipped" | "declined">(
    field.disposition ?? "",
  );
  const [mutationId, setMutationId] = useState(() => createCorrelationId("journal-field"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const metadata = [
    field.unit,
    field.functionId === undefined ? undefined : `Function: ${field.functionId}`,
    field.authorship === undefined ? undefined : `Authorship: ${field.authorship}`,
    field.reviewState === undefined ? undefined : `Review: ${field.reviewState}`,
  ].filter((item): item is string => item !== undefined && item.length > 0);
  const changed = (next: string) => {
    setRaw(next);
    if (error !== undefined) {
      setError(undefined);
      setMutationId(createCorrelationId("journal-field"));
    }
  };
  const save = async () => {
    setError(undefined);
    let value: JournalFieldValue;
    try {
      value = disposition === "" ? typedValue(field, raw) : null;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "That value is invalid.");
      return;
    }
    setBusy(true);
    let result: IntentResult;
    try {
      result = await emit(
        createWidgetIntent(
          presentation,
          "wb.journal.field-value.put",
          ({
            local_date: input.localDate,
            module_instance_id: input.instanceId,
            module_instance_version: input.moduleInstanceVersion,
            composition_slot_id: field.compositionSlotId,
            field_id: field.fieldId,
            field_definition_version: field.definitionVersion,
            ...(field.valueId === undefined ? {} : { value_id: field.valueId }),
            expected_revision: field.valueRevision ?? 0,
            value,
            ...(disposition === "" ? {} : { disposition }),
            exact_input: disposition === "" ? raw : `disposition:${disposition}`,
            stated_at: new Date().toISOString(),
          }) as unknown as JsonValue,
          { clientMutationId: mutationId },
        ) as unknown as JournalFieldValuePutIntent as unknown as WidgetIntent,
      );
    } catch (reason) {
      setBusy(false);
      setError(reason instanceof Error
        ? reason.message
        : "That value could not be saved. Your input is still here.");
      return;
    }
    setBusy(false);
    if (result.status === "accepted") {
      setEditing(false);
      setMutationId(createCorrelationId("journal-field"));
      return;
    }
    setError(result.message ?? (
      result.status === "conflict"
        ? "This field changed. Refresh Journal before trying again."
        : "That value could not be saved. Your input is still here."
    ));
  };
  return (
    <div className="journal-generic-module__field">
      <dt>
        {field.label}
        {field.required ? <span aria-label="required"> *</span> : null}
      </dt>
      {editing ? (
        <dd className="journal-generic-module__editor">
          <FieldControl field={field} raw={raw} disabled={disposition !== "" || busy} onChange={changed} />
          <label>
            Record as
            <select
              value={disposition}
              disabled={busy}
              onChange={(event) => {
                setDisposition(event.target.value as typeof disposition);
                if (error !== undefined) {
                  setError(undefined);
                  setMutationId(createCorrelationId("journal-field"));
                }
              }}
            >
              <option value="">A value</option>
              <option value="missing">Missing</option>
              <option value="skipped">Skipped</option>
              <option value="declined">Prefer not to answer</option>
            </select>
          </label>
          {error ? <InlineAlert tone={error.includes("changed") ? "warning" : "danger"}>{error}</InlineAlert> : null}
          <div className="journal-generic-module__actions">
            <Button variant="ghost" disabled={busy} onClick={() => {
              setEditing(false);
              setRaw(editableText(field));
              setDisposition(field.disposition ?? "");
              setError(undefined);
              setMutationId(createCorrelationId("journal-field"));
            }}>Cancel</Button>
            <Button disabled={busy} onClick={() => void save()}>{busy ? "Saving…" : "Save"}</Button>
          </div>
        </dd>
      ) : (
        <dd>
          {field.disposition === undefined
            ? valueText(field.value)
            : field.disposition[0]!.toLocaleUpperCase() + field.disposition.slice(1)}
          {!field.readOnly ? (
            <button type="button" className="journal-generic-module__edit" onClick={() => setEditing(true)}>
              {field.valueRevision === undefined ? "Add value" : "Edit"}
            </button>
          ) : null}
        </dd>
      )}
      {field.description ? <dd>{field.description}</dd> : null}
      {metadata.length > 0 ? <dd className="journal-generic-module__meta">{metadata.join(" · ")}</dd> : null}
      {field.unavailableReason ? <dd>{field.unavailableReason}</dd> : null}
    </div>
  );
}

const resultError = (result: IntentResult, fallback: string): string | undefined =>
  result.status === "accepted" ? undefined : result.message ?? fallback;

const currentDocument = (value: unknown): JournalDocumentModuleCurrent | undefined => {
  if (typeof value !== "object" || value === null) return undefined;
  const candidate = value as Partial<JournalDocumentModuleCurrent>;
  return candidate.state === "current"
    && typeof candidate.storeId === "string"
    && typeof candidate.documentId === "string"
    && typeof candidate.bindingId === "string"
    && typeof candidate.domainEntityId === "string"
    && typeof candidate.role === "string"
    && typeof candidate.href === "string"
    && candidate.href.startsWith("/app/cowork?")
    && typeof candidate.contentAuthorityEpoch === "number"
    && typeof candidate.canOpenFull === "boolean"
    ? candidate as JournalDocumentModuleCurrent
    : undefined;
};

function OpenJournalDocumentPanel({
  input,
  document,
  primary,
  onClose,
}: {
  readonly input: JournalGenericModuleInput;
  readonly document: JournalDocumentModuleCurrent;
  readonly primary: ReactNode;
  readonly onClose: () => void;
}) {
  const session = useDocumentSession({
    storeId: document.storeId,
    documentId: document.documentId,
    readOnly: input.access.mode === "read_only",
    includeTruthProjection: false,
  });
  const reference = useMemo<BoundDocumentRef>(() => ({
    kind: "domain-bound",
    storeId: document.storeId,
    documentId: document.documentId,
    binding: {
      bindingId: document.bindingId,
      domain: {
        namespace: "journal",
        kind: "document_module",
        entityId: document.domainEntityId,
        role: document.role,
      },
      authorityEpoch: document.contentAuthorityEpoch,
      projectionMode: "none",
    },
  }), [document]);
  return (
    <DocumentWorkspacePanel
      reference={reference}
      session={session}
      primary={primary}
      title={input.label}
      layoutId={`wb.journal.document:${input.localDate}:${input.instanceId}`}
      canOpenFull={document.canOpenFull}
      onClose={onClose}
      onOpenFull={() => window.location.assign(document.href)}
    />
  );
}

function JournalDocumentModule({
  input,
  emit,
  presentation,
}: WidgetRendererProps<JournalGenericModuleInput>) {
  const [panelOpen, setPanelOpen] = useState(false);
  const [opened, setOpened] = useState<JournalDocumentModuleCurrent>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const document = input.document?.state === "current" ? input.document : opened;
  const summary = (
    <section className="journal-generic-module" aria-label={input.label}>
      <header>
        <h3>{input.label}</h3>
        {input.description ? <p>{input.description}</p> : null}
      </header>
      <p>
        This is a Co-work document. Edit provenance is retained, and Truth starts
        disabled until you explicitly enable it in Co-work.
      </p>
      {input.access.mode === "read_only" && input.access.reason ? (
        <InlineAlert tone="warning">{input.access.reason}</InlineAlert>
      ) : null}
      <Button
        disabled={busy || input.access.mode === "read_only"}
        onClick={() => {
          if (document !== undefined) {
            setPanelOpen(true);
            return;
          }
          setBusy(true);
          setError(undefined);
          void emit(createWidgetIntent(
            presentation,
            "wb.journal.document.open",
            {
              local_date: input.localDate,
              module_instance_version: input.moduleInstanceVersion,
            },
            { clientMutationId: createCorrelationId("journal-document-open") },
          )).then((result) => {
            if (result.status !== "accepted") {
              setError(result.message ?? "That Journal document could not be opened.");
              return;
            }
            const target = currentDocument(result.value);
            if (target === undefined) {
              setError("Co-work did not return a valid document target.");
              return;
            }
            setOpened(target);
            setPanelOpen(true);
          }).catch((reason: unknown) => {
            setError(reason instanceof Error
              ? reason.message
              : "That Journal document could not be opened.");
          }).finally(() => setBusy(false));
        }}
      >
        {busy ? "Opening…" : document === undefined ? "Create document" : "Open document"}
      </Button>
      {error ? <InlineAlert tone="danger">{error}</InlineAlert> : null}
    </section>
  );
  return panelOpen && document !== undefined ? (
    <OpenJournalDocumentPanel
      input={input}
      document={document}
      primary={summary}
      onClose={() => setPanelOpen(false)}
    />
  ) : summary;
}

function PromptField({
  field,
  input,
  interaction,
  emit,
  presentation,
}: {
  readonly field: JournalEffectiveFieldInput;
  readonly input: JournalGenericModuleInput;
  readonly interaction?: JournalPromptInteraction;
  readonly emit: (intent: WidgetIntent) => Promise<IntentResult>;
  readonly presentation: WidgetPresentationContext;
}) {
  const [seed, setSeed] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const disabled = input.access.mode === "read_only" || field.readOnly === true || busy;
  const generationAllowed = input.aiContribution === "allowed"
    || input.aiContribution === "suggestion_only";
  const send = async (
    type: "wb.journal.prompt-create" | "wb.journal.prompt-generate" | "wb.journal.prompt-decision",
    payload: JsonValue,
    mutationPrefix: string,
  ) => {
    setBusy(true);
    setError(undefined);
    try {
      const result = await emit(createWidgetIntent(
        presentation,
        type,
        payload,
        { clientMutationId: createCorrelationId(mutationPrefix) },
      ));
      setError(resultError(result, "That prompt action could not be completed."));
      if (result.status === "accepted" && type === "wb.journal.prompt-create") setSeed("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "That prompt action is unavailable.");
    } finally {
      setBusy(false);
    }
  };
  const latestGeneration = interaction === undefined
    ? undefined
    : interaction.generationRequests[interaction.generationRequests.length - 1];
  const generationActive = latestGeneration?.status === "pending"
    || latestGeneration?.status === "leased";
  const generationLabel = latestGeneration?.retryable
    ? "Retry generation"
    : (interaction?.variants.length ?? 0) > 0
      ? "Generate another result"
      : "Generate result";
  return (
    <section className="journal-prompt" aria-label={field.label}>
      <h4>{field.label}</h4>
      {field.description ? <p>{field.description}</p> : null}
      {interaction === undefined ? (
        <div className="journal-prompt__seed-editor">
          <textarea
            aria-label={`${field.label} seed`}
            rows={3}
            value={seed}
            disabled={disabled}
            onChange={(event) => setSeed(event.target.value)}
          />
          <Button
            disabled={disabled || seed.trim().length === 0}
            onClick={() => void send("wb.journal.prompt-create", {
              local_date: input.localDate,
              module_instance_id: input.instanceId,
              module_instance_version: input.moduleInstanceVersion,
              prompt_id: field.promptId!,
              prompt_version: field.promptVersion!,
              exact_input: seed,
            }, "journal-prompt-seed")}
          >{busy ? "Saving…" : "Save seed"}</Button>
        </div>
      ) : (
        <>
          <div className="journal-prompt__seed">
            <strong>Original seed</strong>
            <p>{interaction.inputText}</p>
            <small>Human input · retained separately</small>
          </div>
          {latestGeneration !== undefined ? (
            <p className={`journal-prompt__status journal-prompt__status--${latestGeneration.status}`}>
              Generation: {latestGeneration.status.replace(/_/gu, " ")}
              {latestGeneration.errorCode ? ` (${latestGeneration.errorCode})` : ""}
            </p>
          ) : null}
          {generationAllowed ? (
            <Button
              disabled={disabled || generationActive}
              onClick={() => void send("wb.journal.prompt-generate", {
                interaction_id: interaction.interactionId,
                expected_revision: interaction.currentRevision,
              }, "journal-prompt-generate")}
            >{generationActive ? "Generation pending…" : busy ? "Starting…" : generationLabel}</Button>
          ) : (
            <InlineAlert tone="warning">
              AI generation is disabled by this section&apos;s interaction behavior.
            </InlineAlert>
          )}
          <div className="journal-prompt__variants">
            {interaction.variants.map((variant) => (
              <article key={variant.variantId} className="journal-prompt__variant">
                <strong>Generated result</strong>
                <p>{variant.resultText}</p>
                <small>
                  {variant.authorship} · {variant.reviewState} · {variant.lifecycle}
                  {variant.modelId ? ` · ${variant.modelId}` : ""}
                </small>
                {variant.lifecycle === "current" ? (
                  <div className="journal-generic-module__actions">
                    {(["accept", "archive", "reject"] as const).map((decision) => (
                      <Button
                        key={decision}
                        variant={decision === "accept" ? "primary" : "ghost"}
                        disabled={disabled}
                        onClick={() => void send("wb.journal.prompt-decision", {
                          interaction_id: interaction.interactionId,
                          variant_id: variant.variantId,
                          expected_revision: interaction.currentRevision,
                          decision,
                        }, `journal-prompt-${decision}`)}
                      >{decision[0]!.toUpperCase() + decision.slice(1)}</Button>
                    ))}
                  </div>
                ) : null}
              </article>
            ))}
          </div>
        </>
      )}
      {error ? <InlineAlert tone="danger">{error}</InlineAlert> : null}
    </section>
  );
}

function NativeItem({
  item,
  emit,
  presentation,
}: {
  readonly item: JournalNativeItemInput;
  readonly emit: (intent: WidgetIntent) => Promise<IntentResult>;
  readonly presentation: WidgetPresentationContext;
}) {
  const [action, setAction] = useState<JournalItemAction>();
  const [text, setText] = useState(item.text);
  const [targetDomain, setTargetDomain] = useState("task");
  const [targetId, setTargetId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  useEffect(() => {
    if (action === undefined) setText(item.text);
  }, [action, item.revision, item.text]);
  const submit = async (selected: JournalItemAction) => {
    setBusy(true);
    setError(undefined);
    try {
      const result = await emit(createWidgetIntent(
        presentation,
        "wb.journal.item-action",
        {
          item_id: item.itemId,
          action: selected,
          expected_revision: item.revision,
          ...(selected === "edit" || selected === "correct" ? { exact_text: text } : {}),
          ...(selected === "route" ? { target_domain: targetDomain, target_id: targetId } : {}),
        },
        { clientMutationId: createCorrelationId(`journal-item-${selected}`) },
      ));
      setError(resultError(result, "That item action could not be completed."));
      if (result.status === "accepted") setAction(undefined);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "That item action is unavailable.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <article className="journal-generic-module__item">
      <p>{item.text}</p>
      <small>{item.itemKind.replace(/_/gu, " ")} · {item.authorityKind.replace(/_/gu, " ")} · revision {item.revision}</small>
      {item.relations.filter((relation) => relation.lifecycle === "current").map((relation) => (
        <small key={relation.relationId}>Routed to {relation.targetDomain}: {relation.targetId}</small>
      ))}
      <div className="journal-generic-module__actions">
        {item.actions.map((candidate) => (
          <Button key={candidate} variant="ghost" disabled={busy} onClick={() => {
            if (candidate === "edit" || candidate === "correct" || candidate === "route") setAction(candidate);
            else void submit(candidate);
          }}>{candidate[0]!.toUpperCase() + candidate.slice(1)}</Button>
        ))}
      </div>
      {action === "edit" || action === "correct" ? (
        <div className="journal-generic-module__editor">
          <textarea aria-label={`${action} Journal item`} rows={3} value={text} onChange={(event) => setText(event.target.value)} />
          <Button disabled={busy || text.length === 0} onClick={() => void submit(action)}>Save {action}</Button>
        </div>
      ) : action === "route" ? (
        <div className="journal-generic-module__editor">
          <select aria-label="Route domain" value={targetDomain} onChange={(event) => setTargetDomain(event.target.value)}>
            <option value="task">Task</option>
            <option value="project">Project</option>
            <option value="contract">Contract</option>
            <option value="entity">Entity</option>
            <option value="session">Session</option>
            <option value="calendar_event">Calendar event</option>
            <option value="cowork_document">Co-work document</option>
            <option value="consideration">Consideration</option>
          </select>
          <input aria-label="Route target ID" value={targetId} onChange={(event) => setTargetId(event.target.value)} />
          <Button disabled={busy || targetId.trim().length === 0} onClick={() => void submit("route")}>Save route</Button>
        </div>
      ) : null}
      {error ? <InlineAlert tone="danger">{error}</InlineAlert> : null}
    </article>
  );
}

/** Render one immutable, provider-defined module with Source-backed typed edits. */
export default function JournalGenericModule({
  input,
  emit,
  presentation,
}: WidgetRendererProps<JournalGenericModuleInput>) {
  if (input.moduleTypeId === "document") {
    return <JournalDocumentModule input={input} emit={emit} presentation={presentation} />;
  }
  return (
    <section className="journal-generic-module" aria-label={input.label}>
      <header>
        <h3>{input.label}</h3>
        {input.description ? <p>{input.description}</p> : null}
      </header>
      {input.unavailableReason ? (
        <InlineAlert tone="warning">{input.unavailableReason}</InlineAlert>
      ) : null}
      {input.access.mode === "read_only" && input.access.reason ? (
        <InlineAlert tone="warning">{input.access.reason}</InlineAlert>
      ) : null}
      {input.fields.length === 0 ? (
        <p className="journal-generic-module__empty">
          This section has no fields for this day.
        </p>
      ) : (
        <dl>
          {input.fields.map((field) => input.moduleTypeId === "prompt_result"
            && field.promptId !== undefined
            && field.promptVersion !== undefined ? (
              <PromptField
                key={`${field.fieldId}:${field.definitionVersion}`}
                field={field}
                input={input}
                interaction={input.promptInteractions?.find((candidate) =>
                  candidate.promptId === field.promptId
                  && candidate.promptVersion === field.promptVersion)}
                emit={emit}
                presentation={presentation}
              />
            ) : (
              <FieldValue
                key={`${field.fieldId}:${field.definitionVersion}`}
                field={field}
                input={input}
                emit={emit}
                presentation={presentation}
              />
            ))}
        </dl>
      )}
      {(input.items?.length ?? 0) > 0 ? (
        <div className="journal-generic-module__items">
          {input.items!.map((item) => (
            <NativeItem key={item.itemId} item={item} emit={emit} presentation={presentation} />
          ))}
        </div>
      ) : null}
    </section>
  );
}
