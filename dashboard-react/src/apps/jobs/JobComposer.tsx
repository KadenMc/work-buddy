import { useEffect, useId, useRef, useState } from "react";
import type { IntentResult, JsonValue, WidgetRendererProps } from "../../dashboard/contributions/contracts";
import { useDashboardAnnouncer } from "../../dashboard/accessibility/DashboardAnnouncer";
import { useWidgetDraft } from "../../dashboard/drafts";
import { AssistDraftButton, useAssistedDraft } from "../../dashboard/assistance";
import { Button, InlineAlert, TextAreaField } from "../../ui";
import { createCorrelationId, createWidgetIntent } from "../../widget-library/shared";
import { EMPTY_JOB_DRAFT, JOB_INTENTS, type JobAuthoringInput, type JobCreateDraft } from "./contracts";

export default function JobComposer({ input, emit, presentation }: WidgetRendererProps<JobAuthoringInput>) {
  const draft = useWidgetDraft("job-create", EMPTY_JOB_DRAFT, {
    isPristine: (value) => JSON.stringify(value) === JSON.stringify(EMPTY_JOB_DRAFT),
  });
  const { announce } = useDashboardAnnouncer();
  const formId = useId();
  const fieldId = (key: keyof JobCreateDraft, part: string) => `${formId}-${key}-${part}`;
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [validation, setValidation] = useState<{ values: JobCreateDraft; errors: Readonly<Record<string, string>> } | null>(null);
  const [schedule, setSchedule] = useState<{ valid: boolean; description: string; maximum: number } | null>(null);
  const readOnly = input.access.mode === "read_only" || presentation.interactionMode === "arrange";
  const assistance = useAssistedDraft("job-create", draft, { title: "Help me create a job", interactionMode: presentation.interactionMode, readOnly: readOnly || busy });
  const value = draft.value;
  // Validation describes the submitted values, not later manual/assistant edits.
  const errors = Object.fromEntries(Object.entries(validation?.errors ?? {}).filter(([key]) => {
    if (!validation || value[key as keyof JobCreateDraft] !== validation.values[key as keyof JobCreateDraft]) return false;
    if (["capability", "workflow", "params", "prompt"].includes(key) && value.job_type !== validation.values.job_type) return false;
    return key !== "jitter_seconds" || value.schedule === validation.values.schedule;
  }));
  const openedByLink = useRef(false);
  useEffect(() => {
    if (input.openAssistance && assistance.available && !openedByLink.current) {
      openedByLink.current = true;
      assistance.open();
    }
  }, [input.openAssistance, assistance.available, assistance.open]);
  const emitRef = useRef(emit);
  emitRef.current = emit;
  useEffect(() => {
    if (!value.schedule.trim() || presentation.interactionMode !== "operate") { setSchedule(null); return; }
    let disposed = false;
    const timer = window.setTimeout(() => {
      void emitRef.current(createWidgetIntent(presentation, JOB_INTENTS.describeSchedule, { schedule: value.schedule })).then((result) => {
        if (disposed || result.status !== "accepted") return;
        const preview = result.value as unknown as { valid: boolean; description: string; max_jitter_seconds: number };
        setSchedule({ valid: preview.valid === true, description: preview.description ?? "", maximum: preview.max_jitter_seconds ?? 0 });
      }).catch(() => { if (!disposed) setSchedule(null); });
    }, 250);
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [value.schedule, presentation.instanceId, presentation.viewId, presentation.interactionMode]);
  const update = <Key extends keyof JobCreateDraft>(key: Key, next: JobCreateDraft[Key]) => draft.setValue((current) => ({ ...current, [key]: next }));
  const fieldProps = (key: keyof JobCreateDraft) => ({
    ...assistance.fieldProps([key]), disabled: readOnly || busy,
    "aria-labelledby": fieldId(key, "label"), "aria-invalid": errors[key] ? "true" as const : undefined,
    "aria-describedby": [errors[key] ? fieldId(key, "error") : "", ["schedule", "jitter_seconds", "capability", "workflow"].includes(key) ? fieldId(key, "hint") : ""].filter(Boolean).join(" ") || undefined,
  });
  const hint = (key: keyof JobCreateDraft) => errors[key] ? <small id={fieldId(key, "error")} className="wb-job-error">{errors[key]}</small> : null;
  const registry = value.job_type === "workflow" ? input.workflows : input.capabilities;
  const invokeName = value.job_type === "workflow" ? value.workflow : value.capability;
  const selected = registry.find((entry) => entry.name === invokeName);
  const submit = async () => {
    if (readOnly || busy) return;
    const invalid: Record<string, string> = {};
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(value.name.trim())) invalid.name = "Use 1–64 letters, digits, hyphens or underscores, starting with a letter or digit.";
    if (!value.schedule.trim() || schedule?.valid === false) invalid.schedule = "Enter a valid five-field schedule.";
    if (value.job_type === "prompt" && !value.prompt.trim()) invalid.prompt = "Describe what the job should do.";
    if (value.job_type !== "prompt" && !invokeName.trim()) invalid[value.job_type] = "Choose a registered name.";
    let params: JsonValue = {};
    if (value.job_type !== "prompt") {
      try { params = JSON.parse(value.params || "{}"); if (params === null || typeof params !== "object" || Array.isArray(params)) throw new Error(); }
      catch { invalid.params = "Parameters must be a valid JSON object."; }
    }
    if (!Number.isInteger(value.jitter_seconds) || value.jitter_seconds < 0 || (schedule && value.jitter_seconds > schedule.maximum)) invalid.jitter_seconds = `Use a whole number between 0 and ${schedule?.maximum ?? 300}.`;
    setValidation({ values: value, errors: invalid });
    if (Object.keys(invalid).length > 0) { announce("Review the highlighted job fields.", "assertive"); return; }
    setBusy(true); setNotice(null);
    const revision = draft.revision;
    try {
      await draft.flush();
      const id = createCorrelationId("job-create");
      const result: IntentResult = await emit(createWidgetIntent(presentation, JOB_INTENTS.create, {
        name: value.name.trim(), schedule: value.schedule.trim(), job_type: value.job_type, jitter_seconds: value.jitter_seconds,
        ...(value.job_type === "prompt" ? { prompt: value.prompt } : { [value.job_type]: invokeName.trim(), params }),
      }, { intentId: id, clientMutationId: id }));
      const text = result.message ?? (result.status === "accepted" ? "Job created." : "The job was not created. Your draft is preserved.");
      setNotice({ tone: result.status === "accepted" ? "success" : "danger", text });
      setValidation({ values: value, errors: result.fieldErrors ?? {} });
      announce(text, result.status === "accepted" ? "polite" : "assertive");
      if (result.status === "accepted") await draft.clear({ ifRevision: revision });
    } catch (error) { setNotice({ tone: "danger", text: error instanceof Error ? error.message : "Could not confirm creation. Check existing jobs before retrying. Your draft is preserved." }); }
    finally { setBusy(false); }
  };
  if (!draft.ready) return <p aria-busy="true">Restoring job draft…</p>;
  return <form className="wb-job-composer" onSubmit={(event) => { event.preventDefault(); void submit(); }} noValidate>
    <div className="wb-job-assist"><p>The assistant fills these fields. You review and create the job.</p><AssistDraftButton assistance={assistance} /></div>
    {draft.error ? <InlineAlert tone="danger">{draft.error} Your draft remains here.</InlineAlert> : null}
    {notice ? <InlineAlert tone={notice.tone}>{notice.text}</InlineAlert> : null}
    <div className="wb-job-fields">
      <label><span id={fieldId("name", "label")}>Job name</span><input {...fieldProps("name")} value={value.name} placeholder="weekly-review" onChange={(event) => update("name", event.target.value)} />{hint("name")}</label>
      <label><span id={fieldId("schedule", "label")}>Schedule</span><input {...fieldProps("schedule")} value={value.schedule} placeholder="0 9 * * 1" onChange={(event) => update("schedule", event.target.value)} />{hint("schedule")}<small id={fieldId("schedule", "hint")} aria-live="polite">{schedule ? schedule.valid ? `${schedule.description} · ${input.timeZone}` : "This does not parse as a five-field schedule." : "Ask Assist to turn a plain-English schedule into these fields."}</small></label>
      <label><span id={fieldId("job_type", "label")}>Job type</span><select {...fieldProps("job_type")} value={value.job_type} onChange={(event) => update("job_type", event.target.value as JobCreateDraft["job_type"])}><option value="prompt">Agent prompt</option><option value="capability">Capability</option><option value="workflow">Workflow</option></select></label>
      <label><span id={fieldId("jitter_seconds", "label")}>Jitter (seconds)</span><input {...fieldProps("jitter_seconds")} type="number" min={0} max={schedule?.maximum ?? 300} value={value.jitter_seconds} onChange={(event) => update("jitter_seconds", event.target.value === "" ? 0 : Number(event.target.value))} />{hint("jitter_seconds")}<small id={fieldId("jitter_seconds", "hint")}>{value.jitter_seconds > 0 && value.jitter_seconds < 30 ? "Below 30 seconds may be too small to affect the scheduler tick." : `Optional delay; maximum ${schedule?.maximum ?? 300}s for this schedule.`}</small></label>
    </div>
    {value.job_type === "prompt" ? <TextAreaField {...assistance.fieldProps(["prompt"])} disabled={readOnly || busy} label="What should the job do?" value={value.prompt} rows={5} description={errors.prompt} aria-invalid={errors.prompt ? "true" : undefined} onChange={(next) => update("prompt", next)} /> : <>
      <label className="wb-job-invoke"><span id={fieldId(value.job_type, "label")}>{value.job_type === "workflow" ? "Workflow" : "Capability"}</span><input {...fieldProps(value.job_type)} list={`${formId}-registry`} value={invokeName} onChange={(event) => update(value.job_type === "workflow" ? "workflow" : "capability", event.target.value)} />{hint(value.job_type)}<small id={fieldId(value.job_type, "hint")}>{selected?.description ?? "Choose a registered name; creation validates it."}</small></label>
      <datalist id={`${formId}-registry`}>{registry.map((entry) => <option key={entry.name} value={entry.name}>{entry.description}</option>)}</datalist>
      {selected && Object.keys(selected.parameters).length > 0 ? <details><summary>Expected parameters</summary><ul>{Object.entries(selected.parameters).map(([name, parameter]) => <li key={name}><strong>{name}</strong>{parameter.required ? " (required)" : ""} · {parameter.type} — {parameter.description}</li>)}</ul></details> : null}
      <TextAreaField {...assistance.fieldProps(["params"])} disabled={readOnly || busy} label="Parameters (JSON)" value={value.params} rows={4} description={errors.params} aria-invalid={errors.params ? "true" : undefined} onChange={(next) => update("params", next)} />
    </>}
    <p className="wb-job-confirmation">Create job schedules this as an enabled, recurring job in {input.timeZone}. Chat cannot create it for you.</p>
    <div><Button type="submit" variant="primary" disabled={readOnly || busy}>{busy ? "Creating…" : "Create job"}</Button></div>
  </form>;
}
