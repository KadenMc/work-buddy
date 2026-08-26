/** The Settings-owned default is an atomic pair, never a tier or client label. */
export interface ChatExecutionSettingValue {
  readonly provider_id: string;
  readonly model_id: string;
}

export function isChatExecutionSettingValue(
  value: unknown,
): value is ChatExecutionSettingValue {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return Object.keys(record).length === 2 && ["provider_id", "model_id"].every((key) => {
    const item = record[key];
    return typeof item === "string" && item.length > 0 && item.length <= 256 && item.trim() === item;
  });
}
