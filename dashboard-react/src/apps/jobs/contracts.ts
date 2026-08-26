export interface JobCreateDraft {
  readonly name: string;
  readonly schedule: string;
  readonly job_type: "prompt" | "capability" | "workflow";
  readonly capability: string;
  readonly workflow: string;
  readonly prompt: string;
  readonly params: string;
  readonly jitter_seconds: number;
}

export const EMPTY_JOB_DRAFT: JobCreateDraft = { name: "", schedule: "", job_type: "prompt", capability: "", workflow: "", prompt: "", params: "{}", jitter_seconds: 0 };
export interface JobRegistryEntry {
  readonly name: string;
  readonly description: string;
  readonly parameters: Readonly<Record<string, { readonly type?: string; readonly description?: string; readonly required?: boolean }>>;
}
export interface JobAuthoringInput {
  readonly access: { readonly mode: "read_write" | "read_only"; readonly reason?: string };
  readonly timeZone: string;
  readonly capabilities: readonly JobRegistryEntry[];
  readonly workflows: readonly JobRegistryEntry[];
  readonly openAssistance?: boolean;
}
export const JOB_INTENTS = { create: "wb.jobs.create", describeSchedule: "wb.jobs.schedule.describe" } as const;
