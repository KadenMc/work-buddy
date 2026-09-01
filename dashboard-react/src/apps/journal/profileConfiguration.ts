import { exactHumanAuthorityHeaders } from "../../security/humanAuthority";

export const JOURNAL_CONFIGURATION_ENDPOINT = "/api/journal/configuration" as const;

export type JournalProfileAuthorityState =
  | "legacy_compatibility"
  | "database_only"
  | "recovery_fenced";

export interface JournalProfilePromptDraft {
  readonly promptId: string;
  readonly expectedVersion: number;
  readonly wording: string;
  readonly helpText: string;
  readonly requiredness: "optional" | "required";
  readonly scheduleKind: string;
  readonly schedule: Readonly<Record<string, unknown>>;
}

export interface JournalProfileFieldDraft {
  readonly slotId: string;
  readonly fieldId: string;
  readonly expectedVersion: number;
  readonly owner: string;
  readonly stableKey: string;
  readonly label: string;
  readonly description: string;
  readonly valueKind: string;
  readonly unit: string | null;
  readonly constraints: Readonly<Record<string, unknown>>;
  readonly functionId: string | null;
  readonly functionVersion: number | null;
  readonly behaviorId: string;
  readonly behaviorVersion: number;
  readonly privacyClass: string;
  readonly searchMode: string;
  readonly disclosurePolicyId: string;
  readonly prompt: JournalProfilePromptDraft | null;
}

export interface JournalProfileModuleDraft {
  readonly slotId: string;
  readonly moduleInstanceId: string;
  readonly expectedVersion: number;
  readonly moduleTypeId: string;
  readonly moduleTypeVersion: number;
  readonly label: string;
  readonly settings: Readonly<Record<string, unknown>>;
  readonly behaviorId: string;
  readonly behaviorVersion: number;
  readonly scheduleKind: string;
  readonly schedule: Readonly<Record<string, unknown>>;
  readonly required: boolean;
  readonly fields: readonly JournalProfileFieldDraft[];
}

export interface JournalProfileDraft {
  readonly profileId: string;
  readonly expectedRevision: number;
  readonly name: string;
  readonly description: string;
  readonly modules: readonly JournalProfileModuleDraft[];
}

export type JournalProfilePromptRevisionRecord = Omit<
  JournalProfilePromptDraft,
  "expectedVersion"
> & {
  readonly promptVersion: number;
};

export type JournalProfileFieldRevisionRecord = Omit<
  JournalProfileFieldDraft,
  "expectedVersion" | "prompt"
> & {
  readonly fieldDefinitionVersion: number;
  readonly prompt: JournalProfilePromptRevisionRecord | null;
};

export type JournalProfileModuleRevisionRecord = Omit<
  JournalProfileModuleDraft,
  "expectedVersion" | "fields"
> & {
  readonly ordinal: number;
  readonly moduleInstanceVersion: number;
  readonly fields: readonly JournalProfileFieldRevisionRecord[];
};

export interface JournalProfileRevisionRecord {
  readonly profileId: string;
  readonly profileRevision: number;
  readonly formatVersion: number;
  readonly name: string;
  readonly description: string;
  readonly profileDigest: string;
  readonly createdBy: string;
  readonly createdAt: string;
  readonly supersedesRevision: number | null;
  readonly editable: boolean;
  readonly modules: readonly JournalProfileModuleRevisionRecord[];
}

export interface JournalProfileConfiguration {
  readonly schemaVersion: 1;
  readonly authorityState: JournalProfileAuthorityState;
  readonly activationRevision: number;
  readonly profiles: readonly JournalProfileRevisionRecord[];
  readonly moduleTypes: readonly {
    readonly moduleTypeId: string;
    readonly moduleTypeVersion: number;
    readonly definition: Readonly<Record<string, unknown>>;
  }[];
  readonly behaviors: readonly {
    readonly behaviorId: string;
    readonly behaviorVersion: number;
    readonly definition: Readonly<Record<string, unknown>>;
  }[];
  readonly functions: readonly JournalFunctionCatalogEntry[];
  readonly valueKinds: readonly string[];
  readonly scheduleKinds: readonly string[];
}

export interface JournalFunctionCatalogEntry {
  readonly functionId: string;
  readonly functionVersion: number;
  readonly valueKind: string;
  readonly unit: string | null;
  readonly cardinality: "single" | "multiple";
  readonly definition: Readonly<Record<string, unknown>>;
}

export interface JournalProfilePreview {
  readonly schemaVersion: 1;
  readonly localDate: string;
  readonly profile: { readonly profileId: string; readonly name: string; readonly description: string };
  readonly modules: readonly {
    readonly slotId: string;
    readonly ordinal: number;
    readonly moduleInstanceId: string;
    readonly moduleTypeId: string;
    readonly label: string;
    readonly semanticMembership: "included" | "excluded_by_schedule" | "unavailable";
    readonly fields: readonly {
      readonly slotId: string;
      readonly fieldId: string;
      readonly label: string;
      readonly description: string;
      readonly valueKind: string;
      readonly unit: string | null;
      readonly functionId: string | null;
      readonly functionVersion: number | null;
      readonly promptWording: string | null;
      readonly promptHelp: string | null;
      readonly requiredness: string;
    }[];
  }[];
}

export interface JournalProfileConfigurationProvider {
  load(): Promise<JournalProfileConfiguration>;
  preview(draft: JournalProfileDraft, localDate: string): Promise<JournalProfilePreview>;
  save(draft: JournalProfileDraft): Promise<{
    readonly profileId: string;
    readonly profileRevision: number;
    readonly profileDigest: string;
    readonly activationRevision: number;
  }>;
  activate(input: {
    readonly profileId: string;
    readonly profileRevision: number;
    readonly expectedActivationRevision: number;
    readonly effectiveLocalDate: string;
  }): Promise<{ readonly activationRevision: number; readonly effectiveLocalDate: string }>;
}

const parseJson = async (response: Response): Promise<Record<string, unknown>> => {
  const value = await response.json() as Record<string, unknown>;
  if (!response.ok || value.ok !== true) {
    const error = value.error as { message?: unknown } | undefined;
    throw new Error(typeof error?.message === "string" ? error.message : "Journal configuration failed.");
  }
  return value;
};

const mutationId = (): string => {
  const value = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `journal-profile-${value}`;
};

export class JournalProfileConfigurationClient implements JournalProfileConfigurationProvider {
  readonly #fetch: typeof fetch;

  constructor(fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.#fetch = fetchImpl;
  }

  async load(): Promise<JournalProfileConfiguration> {
    const response = await this.#fetch(JOURNAL_CONFIGURATION_ENDPOINT, {
      credentials: "same-origin",
      cache: "no-store",
    });
    const value = await parseJson(response);
    return value.configuration as unknown as JournalProfileConfiguration;
  }

  async preview(draft: JournalProfileDraft, localDate: string): Promise<JournalProfilePreview> {
    const response = await this.#fetch(`${JOURNAL_CONFIGURATION_ENDPOINT}/preview`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft, localDate }),
    });
    const value = await parseJson(response);
    return value.preview as unknown as JournalProfilePreview;
  }

  async save(draft: JournalProfileDraft): Promise<{
    readonly profileId: string;
    readonly profileRevision: number;
    readonly profileDigest: string;
    readonly activationRevision: number;
  }> {
    const body = { clientMutationId: mutationId(), draft };
    const headers = await exactHumanAuthorityHeaders(
      { action: "journal.profile.save", subject: `journal-profile:${draft.profileId}`, context: body },
      this.#fetch,
    );
    const response = await this.#fetch(`${JOURNAL_CONFIGURATION_ENDPOINT}/profiles`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
    });
    const value = await parseJson(response);
    return value.profile as never;
  }

  async activate(input: {
    readonly profileId: string;
    readonly profileRevision: number;
    readonly expectedActivationRevision: number;
    readonly effectiveLocalDate: string;
  }): Promise<{ readonly activationRevision: number; readonly effectiveLocalDate: string }> {
    const body = {
      clientMutationId: mutationId(),
      expectedActivationRevision: input.expectedActivationRevision,
      effectiveLocalDate: input.effectiveLocalDate,
    };
    const subject = `journal-profile:${input.profileId}:${input.profileRevision}`;
    const headers = await exactHumanAuthorityHeaders(
      { action: "journal.profile.activate", subject, context: body },
      this.#fetch,
    );
    const response = await this.#fetch(
      `${JOURNAL_CONFIGURATION_ENDPOINT}/profiles/${encodeURIComponent(input.profileId)}/${input.profileRevision}/activate`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...headers },
        body: JSON.stringify(body),
      },
    );
    const value = await parseJson(response);
    return value.activation as never;
  }
}

const id = (kind: string): string =>
  `user.${kind}.${(globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`)
    .replace(/-/gu, "")}`;

export function editProfileDraft(profile: JournalProfileRevisionRecord): JournalProfileDraft {
  if (!profile.editable) {
    throw new Error("This Journal profile is read-only. Fork it before editing.");
  }
  return {
    profileId: profile.profileId,
    expectedRevision: profile.profileRevision,
    name: profile.name,
    description: profile.description,
    modules: profile.modules.map((module) => ({
      slotId: module.slotId,
      moduleInstanceId: module.moduleInstanceId,
      expectedVersion: module.moduleInstanceVersion,
      moduleTypeId: module.moduleTypeId,
      moduleTypeVersion: module.moduleTypeVersion,
      label: module.label,
      settings: module.settings,
      behaviorId: module.behaviorId,
      behaviorVersion: module.behaviorVersion,
      scheduleKind: module.scheduleKind,
      schedule: module.schedule,
      required: module.required,
      fields: module.fields.map((field) => ({
        slotId: field.slotId,
        fieldId: field.fieldId,
        expectedVersion: field.fieldDefinitionVersion,
        owner: field.owner,
        stableKey: field.stableKey,
        label: field.label,
        description: field.description,
        valueKind: field.valueKind,
        unit: field.unit,
        constraints: field.constraints,
        functionId: field.functionId,
        functionVersion: field.functionVersion,
        behaviorId: field.behaviorId,
        behaviorVersion: field.behaviorVersion,
        privacyClass: field.privacyClass,
        searchMode: field.searchMode,
        disclosurePolicyId: field.disclosurePolicyId,
        prompt: field.prompt === null ? null : {
          promptId: field.prompt.promptId,
          expectedVersion: field.prompt.promptVersion,
          wording: field.prompt.wording,
          helpText: field.prompt.helpText,
          requiredness: field.prompt.requiredness,
          scheduleKind: field.prompt.scheduleKind,
          schedule: field.prompt.schedule,
        },
      })),
    })),
  };
}

export function cloneProfileDraft(profile: JournalProfileRevisionRecord): JournalProfileDraft {
  return {
    profileId: id("profile"),
    expectedRevision: 0,
    name: `${profile.name} copy`,
    description: profile.description,
    modules: profile.modules.map((module) => ({
      slotId: id("slot"),
      moduleInstanceId: id("module"),
      expectedVersion: 0,
      moduleTypeId: module.moduleTypeId,
      moduleTypeVersion: module.moduleTypeVersion,
      label: module.label,
      settings: module.settings,
      behaviorId: module.behaviorId ?? "human_value",
      behaviorVersion: module.behaviorVersion ?? 1,
      scheduleKind: module.scheduleKind,
      schedule: module.schedule,
      required: module.required,
      fields: module.fields.map((field) => ({
        slotId: id("field-slot"),
        fieldId: id("field"),
        expectedVersion: 0,
        owner: "user",
        stableKey: id("key"),
        label: field.label,
        description: field.description,
        valueKind: field.valueKind,
        unit: field.unit,
        constraints: field.constraints,
        functionId: field.functionId,
        functionVersion: field.functionVersion,
        behaviorId: field.behaviorId,
        behaviorVersion: field.behaviorVersion,
        privacyClass: field.privacyClass,
        searchMode: field.searchMode,
        disclosurePolicyId: field.disclosurePolicyId,
        prompt: field.prompt === null ? null : {
          promptId: id("prompt"),
          expectedVersion: 0,
          wording: field.prompt.wording,
          helpText: field.prompt.helpText,
          requiredness: field.prompt.requiredness,
          scheduleKind: field.prompt.scheduleKind,
          schedule: field.prompt.schedule,
        },
      })),
    })),
  };
}

export const createEmptyModule = (
  moduleTypeId: string,
  moduleTypeVersion: number,
): JournalProfileModuleDraft => ({
  slotId: id("slot"),
  moduleInstanceId: id("module"),
  expectedVersion: 0,
  moduleTypeId,
  moduleTypeVersion,
  label: moduleTypeId.replace(/_/gu, " ").replace(/^./u, (value: string) => value.toUpperCase()),
  settings: moduleTypeId === "document" ? {
    documentRole: "journal_document",
    truthEligibility: "allowed",
    initialTruthActivation: "disabled",
  } : {},
  behaviorId: moduleTypeId === "prompt_result" || moduleTypeId === "document"
    ? "provenance_only"
    : "human_value",
  behaviorVersion: 1,
  scheduleKind: "always",
  schedule: {},
  required: false,
  fields: [],
});

export const createEmptyField = (): JournalProfileFieldDraft => ({
  slotId: id("field-slot"),
  fieldId: id("field"),
  expectedVersion: 0,
  owner: "user",
  stableKey: id("key"),
  label: "New field",
  description: "",
  valueKind: "short_text",
  unit: null,
  constraints: {},
  functionId: null,
  functionVersion: null,
  behaviorId: "human_value",
  behaviorVersion: 1,
  privacyClass: "private",
  searchMode: "structured_only",
  disclosurePolicyId: "private_default/v1",
  prompt: null,
});

export const createEmptyPrompt = (): JournalProfilePromptDraft => ({
  promptId: id("prompt"),
  expectedVersion: 0,
  wording: "What would you like to record?",
  helpText: "",
  requiredness: "optional",
  scheduleKind: "always",
  schedule: {},
});

export const duplicateModuleDraft = (
  module: JournalProfileModuleDraft,
): JournalProfileModuleDraft => {
  const base = createEmptyModule(module.moduleTypeId, module.moduleTypeVersion);
  return {
    ...base,
    label: `${module.label} copy`,
    settings: module.settings,
    behaviorId: module.behaviorId,
    behaviorVersion: module.behaviorVersion,
    scheduleKind: module.scheduleKind,
    schedule: module.schedule,
    required: false,
    fields: module.fields.map((field) => ({
      ...field,
      slotId: id("field-slot"),
      fieldId: id("field"),
      expectedVersion: 0,
      stableKey: id("key"),
      prompt: field.prompt === null ? null : {
        ...field.prompt,
        promptId: id("prompt"),
        expectedVersion: 0,
      },
    })),
  };
};
