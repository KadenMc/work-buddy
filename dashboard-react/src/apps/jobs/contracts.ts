export interface JobCreateDraft {
  readonly name: string;
  readonly schedule: string;
  readonly job_type: "prompt" | "skill" | "workflow";
  readonly skill: string;
  readonly workflow: string;
  readonly prompt: string;
  readonly params: string;
  readonly jitter_seconds: number;
}

export const EMPTY_JOB_DRAFT: JobCreateDraft = { name: "", schedule: "", job_type: "prompt", skill: "", workflow: "", prompt: "", params: "{}", jitter_seconds: 0 };

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, fallback = "") => typeof value === "string" ? value : fallback;

/** Normalize device-persisted `{job_type: "capability", capability}` drafts. */
export const normalizeJobCreateDraft = (stored: unknown): JobCreateDraft => {
  if (!record(stored)) return EMPTY_JOB_DRAFT;
  const storedType = stored.job_type;
  const jobType: JobCreateDraft["job_type"] = storedType === "capability"
    ? "skill"
    : storedType === "skill" || storedType === "workflow" || storedType === "prompt"
      ? storedType
      : "prompt";
  return {
    name: text(stored.name),
    schedule: text(stored.schedule),
    job_type: jobType,
    skill: text(stored.skill, text(stored.capability)),
    workflow: text(stored.workflow),
    prompt: text(stored.prompt),
    params: text(stored.params, "{}"),
    jitter_seconds: typeof stored.jitter_seconds === "number" ? stored.jitter_seconds : 0,
  };
};

export const jobCreateDraftNeedsNormalization = (stored: unknown): boolean =>
  record(stored) && (stored.job_type === "capability" || Object.prototype.hasOwnProperty.call(stored, "capability") || !Object.prototype.hasOwnProperty.call(stored, "skill"));
export interface JobRegistryEntry {
  readonly name: string;
  readonly description: string;
  readonly parameters: Readonly<Record<string, { readonly type?: string; readonly description?: string; readonly required?: boolean }>>;
}
export interface JobAuthoringInput {
  readonly access: { readonly mode: "read_write" | "read_only"; readonly reason?: string };
  readonly timeZone: string;
  readonly skills: readonly JobRegistryEntry[];
  readonly workflows: readonly JobRegistryEntry[];
  readonly openAssistance?: boolean;
}
export const JOB_INTENTS = { create: "wb.jobs.create", describeSchedule: "wb.jobs.schedule.describe" } as const;
