import { useEffect, useMemo, useState } from "react";

import { Button, InlineAlert } from "../../ui";
import {
  JournalProfileConfigurationClient,
  cloneProfileDraft,
  createEmptyField,
  createEmptyModule,
  createEmptyPrompt,
  duplicateModuleDraft,
  editProfileDraft,
  type JournalProfileConfiguration,
  type JournalProfileConfigurationProvider,
  type JournalProfileDraft,
  type JournalProfileFieldDraft,
  type JournalFunctionCatalogEntry,
  type JournalProfileModuleDraft,
  type JournalProfilePreview,
} from "./profileConfiguration";
import "./profileConfiguration.css";

type LocalCalendarDate = Pick<Date, "getFullYear" | "getMonth" | "getDate">;

export const nextLocalCalendarDate = (now: LocalCalendarDate = new Date()): string => {
  const next = new Date(0);
  next.setUTCFullYear(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  return [
    String(next.getUTCFullYear()).padStart(4, "0"),
    String(next.getUTCMonth() + 1).padStart(2, "0"),
    String(next.getUTCDate()).padStart(2, "0"),
  ].join("-");
};

const DEFAULT_CLIENT = new JournalProfileConfigurationClient();

const replaceModule = (
  draft: JournalProfileDraft,
  index: number,
  module: JournalProfileModuleDraft,
): JournalProfileDraft => ({
  ...draft,
  modules: draft.modules.map((item, candidate) => candidate === index ? module : item),
});

const replaceField = (
  module: JournalProfileModuleDraft,
  index: number,
  field: JournalProfileFieldDraft,
): JournalProfileModuleDraft => ({
  ...module,
  fields: module.fields.map((item, candidate) => candidate === index ? field : item),
});

function FieldEditor({
  field,
  valueKinds,
  behaviorIds,
  functions,
  onChange,
  onRemove,
}: {
  readonly field: JournalProfileFieldDraft;
  readonly valueKinds: readonly string[];
  readonly behaviorIds: readonly string[];
  readonly functions: readonly JournalFunctionCatalogEntry[];
  readonly onChange: (field: JournalProfileFieldDraft) => void;
  readonly onRemove: () => void;
}) {
  return (
    <fieldset className="journal-profile-field">
      <legend>{field.label}</legend>
      <label>
        Field label
        <input value={field.label} onChange={(event) => onChange({ ...field, label: event.target.value })} />
      </label>
      <label>
        Type
        <select value={field.valueKind} onChange={(event) => onChange({
          ...field,
          valueKind: event.target.value,
          functionId: null,
          functionVersion: null,
        })}>
          {valueKinds.map((kind) => <option key={kind} value={kind}>{kind.replace(/_/gu, " ")}</option>)}
        </select>
      </label>
      <label>
        Meaning (optional)
        <select
          value={field.functionId === null ? "" : `${field.functionId}@${field.functionVersion}`}
          onChange={(event) => {
            const selected = functions.find(
              (item) => `${item.functionId}@${item.functionVersion}` === event.target.value,
            );
            onChange(selected ? {
              ...field,
              functionId: selected.functionId,
              functionVersion: selected.functionVersion,
              valueKind: selected.valueKind,
              unit: selected.unit,
            } : { ...field, functionId: null, functionVersion: null });
          }}
        >
          <option value="">Custom field</option>
          {functions.map((item) => {
            const label = typeof item.definition.label === "string"
              ? item.definition.label
              : item.functionId;
            return (
              <option key={`${item.functionId}@${item.functionVersion}`} value={`${item.functionId}@${item.functionVersion}`}>
                {label} ({item.valueKind}{item.unit ? `, ${item.unit}` : ""})
              </option>
            );
          })}
        </select>
      </label>
      <label>
        Help text
        <input value={field.description} onChange={(event) => onChange({ ...field, description: event.target.value })} />
      </label>
      <label>
        Unit (optional)
        <input value={field.unit ?? ""} onChange={(event) => onChange({
          ...field,
          unit: event.target.value || null,
          functionId: null,
          functionVersion: null,
        })} />
      </label>
      <label>
        How AI helps
        <select value={field.behaviorId} onChange={(event) => onChange({ ...field, behaviorId: event.target.value })}>
          {behaviorIds.map((behavior) => <option key={behavior} value={behavior}>{behavior.replace(/_/gu, " ")}</option>)}
        </select>
      </label>
      <label className="journal-profile-checkbox">
        <input
          type="checkbox"
          checked={field.prompt !== null}
          onChange={(event) => onChange({
            ...field,
            prompt: event.target.checked ? createEmptyPrompt() : null,
          })}
        />
        Ask with a prompt
      </label>
      {field.prompt ? (
        <div className="journal-profile-prompt">
          <label>
            Prompt
            <input
              value={field.prompt.wording}
              onChange={(event) => onChange({
                ...field,
                prompt: { ...field.prompt!, wording: event.target.value },
              })}
            />
          </label>
          <label>
            Prompt help
            <input
              value={field.prompt.helpText}
              onChange={(event) => onChange({
                ...field,
                prompt: { ...field.prompt!, helpText: event.target.value },
              })}
            />
          </label>
          <label className="journal-profile-checkbox">
            <input
              type="checkbox"
              checked={field.prompt.requiredness === "required"}
              onChange={(event) => onChange({
                ...field,
                prompt: {
                  ...field.prompt!,
                  requiredness: event.target.checked ? "required" : "optional",
                },
              })}
            />
            Required on scheduled days
          </label>
        </div>
      ) : null}
      <Button variant="ghost" onClick={onRemove}>Remove field</Button>
    </fieldset>
  );
}

function ModuleEditor({
  module,
  index,
  count,
  valueKinds,
  behaviorIds,
  functions,
  onChange,
  onMove,
  onDuplicate,
  onRemove,
}: {
  readonly module: JournalProfileModuleDraft;
  readonly index: number;
  readonly count: number;
  readonly valueKinds: readonly string[];
  readonly behaviorIds: readonly string[];
  readonly functions: readonly JournalFunctionCatalogEntry[];
  readonly onChange: (module: JournalProfileModuleDraft) => void;
  readonly onMove: (delta: -1 | 1) => void;
  readonly onDuplicate: () => void;
  readonly onRemove: () => void;
}) {
  const weekdays = Array.isArray(module.schedule.weekdays)
    ? module.schedule.weekdays as number[]
    : [];
  return (
    <article className="journal-profile-module">
      <header>
        <div><strong>{module.label}</strong><span>{module.moduleTypeId.replace(/_/gu, " ")}</span></div>
        <div className="journal-profile-actions">
          <button type="button" aria-label={`Move ${module.label} up`} disabled={index === 0} onClick={() => onMove(-1)}>↑</button>
          <button type="button" aria-label={`Move ${module.label} down`} disabled={index === count - 1} onClick={() => onMove(1)}>↓</button>
          <button type="button" onClick={onDuplicate}>Duplicate</button>
          <button type="button" onClick={onRemove}>Remove</button>
        </div>
      </header>
      <div className="journal-profile-grid">
        <label>
          Section name
          <input value={module.label} onChange={(event) => onChange({ ...module, label: event.target.value })} />
        </label>
        <label>
          Who writes this?
          <select value={module.behaviorId} onChange={(event) => onChange({ ...module, behaviorId: event.target.value })}>
            {behaviorIds.map((behavior) => <option key={behavior} value={behavior}>{behavior.replace(/_/gu, " ")}</option>)}
          </select>
        </label>
        <label>
          Schedule
          <select
            value={module.scheduleKind}
            onChange={(event) => onChange({
              ...module,
              scheduleKind: event.target.value,
              schedule: event.target.value === "weekdays" ? { weekdays: [0, 1, 2, 3, 4] } : {},
            })}
          >
            <option value="always">Every day</option>
            <option value="weekdays">Selected weekdays</option>
            <option value="manual_only">Only when opened manually</option>
            <option value="date_range">Date range</option>
          </select>
        </label>
      </div>
      {module.scheduleKind === "weekdays" ? (
        <fieldset className="journal-profile-weekdays">
          <legend>Days included</legend>
          {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((label, day) => (
            <label key={label}>
              <input
                type="checkbox"
                checked={weekdays.includes(day)}
                onChange={(event) => onChange({
                  ...module,
                  schedule: {
                    weekdays: event.target.checked
                      ? [...weekdays, day].sort()
                      : weekdays.filter((value) => value !== day),
                  },
                })}
              />
              {label}
            </label>
          ))}
        </fieldset>
      ) : null}
      {module.scheduleKind === "date_range" ? (
        <div className="journal-profile-grid">
          <label>From<input type="date" value={String(module.schedule.start ?? "")} onChange={(event) => onChange({ ...module, schedule: { ...module.schedule, start: event.target.value } })} /></label>
          <label>Through<input type="date" value={String(module.schedule.end ?? "")} onChange={(event) => onChange({ ...module, schedule: { ...module.schedule, end: event.target.value } })} /></label>
        </div>
      ) : null}
      {module.fields.map((field, fieldIndex) => (
        <FieldEditor
          key={field.slotId}
          field={field}
          valueKinds={valueKinds}
          behaviorIds={behaviorIds}
          functions={functions}
          onChange={(value) => onChange(replaceField(module, fieldIndex, value))}
          onRemove={() => onChange({ ...module, fields: module.fields.filter((_, candidate) => candidate !== fieldIndex) })}
        />
      ))}
      {module.moduleTypeId === "field_group" || module.moduleTypeId === "prompt_result" ? (
        <Button variant="ghost" onClick={() => onChange({ ...module, fields: [...module.fields, createEmptyField()] })}>
          Add field
        </Button>
      ) : null}
    </article>
  );
}

export function JournalProfileConfigurator({
  client = DEFAULT_CLIENT,
}: {
  readonly client?: JournalProfileConfigurationProvider;
}) {
  const [configuration, setConfiguration] = useState<JournalProfileConfiguration>();
  const [draft, setDraft] = useState<JournalProfileDraft>();
  const [preview, setPreview] = useState<JournalProfilePreview>();
  const [previewDate, setPreviewDate] = useState(nextLocalCalendarDate);
  const [activationDate, setActivationDate] = useState(nextLocalCalendarDate);
  const [moduleType, setModuleType] = useState("field_group");
  const [saved, setSaved] = useState<{ profileId: string; profileRevision: number }>();
  const [message, setMessage] = useState<string>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  const reload = async () => {
    setConfiguration(await client.load());
  };
  useEffect(() => {
    let active = true;
    void client.load().then((value) => active && setConfiguration(value)).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Journal configuration is unavailable.");
    });
    return () => { active = false; };
  }, [client]);

  const latestProfiles = useMemo(() => {
    const seen = new Set<string>();
    return (configuration?.profiles ?? []).filter((profile) => {
      if (seen.has(profile.profileId)) return false;
      seen.add(profile.profileId);
      return true;
    });
  }, [configuration]);
  const behaviorIds = configuration?.behaviors.map((behavior) => behavior.behaviorId) ?? ["human_value"];
  const readOnly = configuration?.authorityState === "recovery_fenced";
  const beginDraft = (nextDraft: JournalProfileDraft) => {
    setDraft(nextDraft);
    setPreview(undefined);
    setSaved(undefined);
    setMessage(undefined);
    setError(undefined);
  };

  if (!configuration && !error) return <p>Loading Journal profiles…</p>;
  return (
    <section className="journal-profile-configurator" aria-labelledby="journal-profile-title">
      <header>
        <p className="wb-settings-content__eyebrow">Journal composition</p>
        <h2 id="journal-profile-title">Configure Journal</h2>
        <p>Start from a profile, shape its sections and prompts, preview a day, then choose a future date. Existing days keep the composition they already use.</p>
      </header>
      {readOnly ? <InlineAlert tone="warning">Journal recovery is in progress. Profiles are available for review only.</InlineAlert> : null}
      {error ? <InlineAlert tone="danger">{error}</InlineAlert> : null}
      {message ? <InlineAlert tone="success">{message}</InlineAlert> : null}
      {!draft ? (
        <div className="journal-profile-picker">
          {latestProfiles.map((profile) => (
            <article key={profile.profileId}>
              <h3>{profile.name}</h3>
              <p>{profile.description}</p>
              <small>{profile.modules.length} sections · revision {profile.profileRevision}</small>
              <div className="journal-profile-picker-actions">
                {profile.editable ? (
                  <Button disabled={readOnly} onClick={() => beginDraft(editProfileDraft(profile))}>
                    Edit profile
                  </Button>
                ) : null}
                <Button
                  variant={profile.editable ? "ghost" : undefined}
                  disabled={readOnly}
                  onClick={() => beginDraft(cloneProfileDraft(profile))}
                >
                  Fork as new profile
                </Button>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <div className="journal-profile-editor">
          <div className="journal-profile-grid">
            <label>Profile name<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
            <label>Description<input value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></label>
          </div>
          {draft.modules.map((module, index) => (
            <ModuleEditor
              key={module.slotId}
              module={module}
              index={index}
              count={draft.modules.length}
              valueKinds={configuration?.valueKinds ?? []}
              behaviorIds={behaviorIds}
              functions={configuration?.functions ?? []}
              onChange={(value) => setDraft(replaceModule(draft, index, value))}
              onMove={(delta) => {
                const next = [...draft.modules];
                const target = index + delta;
                [next[index], next[target]] = [next[target]!, next[index]!];
                setDraft({ ...draft, modules: next });
              }}
              onDuplicate={() => {
                const clone = duplicateModuleDraft(module);
                setDraft({ ...draft, modules: [...draft.modules.slice(0, index + 1), clone, ...draft.modules.slice(index + 1)] });
              }}
              onRemove={() => setDraft({ ...draft, modules: draft.modules.filter((_, candidate) => candidate !== index) })}
            />
          ))}
          <div className="journal-profile-add">
            <label>
              Add a section
              <input list="journal-module-types" value={moduleType} onChange={(event) => setModuleType(event.target.value)} />
              <datalist id="journal-module-types">
                {configuration?.moduleTypes.map((type) => <option key={type.moduleTypeId} value={type.moduleTypeId} />)}
              </datalist>
            </label>
            <Button variant="ghost" onClick={() => {
              const selected = configuration?.moduleTypes.find((type) => type.moduleTypeId === moduleType);
              if (selected) setDraft({ ...draft, modules: [...draft.modules, createEmptyModule(selected.moduleTypeId, selected.moduleTypeVersion)] });
            }}>Add section</Button>
          </div>
          <div className="journal-profile-preview-controls">
            <label>Preview date<input type="date" value={previewDate} onChange={(event) => setPreviewDate(event.target.value)} /></label>
            <Button variant="ghost" disabled={busy} onClick={() => {
              setBusy(true); setError(undefined);
              void client.preview(draft, previewDate).then(setPreview).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Preview failed.")).finally(() => setBusy(false));
            }}>Preview day</Button>
          </div>
          {preview ? (
            <div className="journal-profile-preview" aria-live="polite">
              <h3>Preview for {preview.localDate}</h3>
              {preview.modules.map((module) => (
                <article key={module.slotId} data-membership={module.semanticMembership}>
                  <strong>{module.label}</strong>
                  <span>{module.semanticMembership.replace(/_/gu, " ")}</span>
                  {module.fields.map((field) => (
                    <p key={field.slotId}>
                      {field.promptWording ?? field.label} · {field.valueKind.replace(/_/gu, " ")}
                      {field.functionId ? ` · ${field.functionId}` : ""}
                    </p>
                  ))}
                </article>
              ))}
            </div>
          ) : null}
          <div className="journal-profile-commit">
            <Button variant="ghost" onClick={() => { setDraft(undefined); setPreview(undefined); }}>Discard draft</Button>
            <Button disabled={busy || readOnly} onClick={() => {
              setBusy(true); setError(undefined); setMessage(undefined);
              void client.save(draft).then((result) => {
                setSaved(result);
                setMessage(`Saved ${draft.name} revision ${result.profileRevision}. It is not active yet.`);
                return reload();
              }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Profile save failed.")).finally(() => setBusy(false));
            }}>{draft.expectedRevision > 0 ? "Save revision" : "Save new profile"}</Button>
          </div>
          {saved ? (
            <div className="journal-profile-activation">
              <label>Use for new days starting<input type="date" min={nextLocalCalendarDate()} value={activationDate} onChange={(event) => setActivationDate(event.target.value)} /></label>
              <Button disabled={busy || readOnly} onClick={() => {
                setBusy(true); setError(undefined); setMessage(undefined);
                void client.activate({ ...saved, expectedActivationRevision: configuration!.activationRevision, effectiveLocalDate: activationDate }).then((result) => {
                  setMessage(`Scheduled for ${result.effectiveLocalDate}. Existing days are unchanged.`);
                  setDraft(undefined); setSaved(undefined); setPreview(undefined);
                  return reload();
                }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Activation failed.")).finally(() => setBusy(false));
              }}>Schedule profile</Button>
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}

export default JournalProfileConfigurator;
