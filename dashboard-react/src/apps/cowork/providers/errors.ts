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

const LEGACY_ERROR_CODE = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;

const legacyErrorString = (payload: unknown): string | null => {
  const outer = objectRecord(payload);
  const value = typeof outer?.error === "string" ? outer.error : payload;
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length === 0 ? null : trimmed;
};

export const normalizeCoworkError = (
  payload: unknown,
  status?: number,
  fallback = "Co-work could not complete that request.",
): CoworkApiError => {
  const outer = objectRecord(payload);
  const nested = objectRecord(outer?.error);
  const source = nested ?? outer;
  const legacyError = legacyErrorString(payload);
  const legacyErrorIsCode =
    legacyError !== null && LEGACY_ERROR_CODE.test(legacyError);
  const code =
    typeof source?.code === "string"
      ? source.code
      : legacyErrorIsCode
        ? legacyError
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
        : legacyError !== null && !legacyErrorIsCode
          ? legacyError
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

const TECHNICAL_ERROR_LANGUAGE =
  /\b(?:Y\.?Doc|snapshot|sha(?:256)?|hash|canonical|structured head|generation|offset|store[_ ]id|scope_root|provenance)\b/i;

/** Keep diagnostics in the error object while preventing storage internals from reaching UI copy. */
export const coworkErrorMessage = (
  error: Pick<CoworkApiError, "message">,
  fallback: string,
): string => {
  const message = error.message.trim();
  if (message.length === 0 || TECHNICAL_ERROR_LANGUAGE.test(message)) return fallback;
  return message;
};
