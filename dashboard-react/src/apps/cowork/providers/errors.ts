import type { CoworkApiError } from "../contracts";

export class CoworkHttpError extends Error {
  readonly apiError: CoworkApiError;

  constructor(apiError: CoworkApiError) {
    super(apiError.message);
    this.name = "CoworkHttpError";
    this.apiError = apiError;
  }
}

const objectRecord = (value: unknown): Record<string, unknown> | null =>
  typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;

export const normalizeCoworkError = (
  payload: unknown,
  status?: number,
  fallback = "Co-work could not complete that request.",
): CoworkApiError => {
  const outer = objectRecord(payload);
  const nested = objectRecord(outer?.error);
  const source = nested ?? outer;
  const rawError = outer?.error;
  const code =
    typeof source?.code === "string"
      ? source.code
      : typeof rawError === "string"
        ? rawError
        : status === 404
          ? "not_found"
          : status === 403
            ? "dashboard_read_only"
            : "request_failed";
  const message =
    typeof source?.message === "string"
      ? source.message
      : typeof outer?.message === "string"
        ? outer.message
        : fallback;
  return {
    code,
    message,
    ...(typeof source?.field === "string" ? { field: source.field } : {}),
    retryable:
      typeof source?.retryable === "boolean"
        ? source.retryable
        : status === undefined || status >= 500,
    ...(objectRecord(source?.details) === null
      ? {}
      : { details: objectRecord(source?.details) ?? undefined }),
    ...(status === undefined ? {} : { status }),
  };
};
export const asCoworkApiError = (error: unknown): CoworkApiError => {
  if (error instanceof CoworkHttpError) return error.apiError;
  if (error instanceof Error) {
    return {
      code: "network_error",
      message: error.message,
      retryable: true,
    };
  }
  return {
    code: "network_error",
    message: String(error),
    retryable: true,
  };
};
