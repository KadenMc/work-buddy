export const JOURNAL_BROWSER_BUDGET = {
  domContentLoadedMs: 10_000,
  // The development-server trace includes unminified React Aria modules.
  // Production asset size remains a separate build gate.
  decodedScriptAndStyleBytes: 4_500_000,
  longTaskCount: 20,
} as const;
