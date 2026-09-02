import type {
  CoworkDocumentSummary,
  CoworkTruthActivation,
} from "./contracts";
import type { RailTab } from "./rail";

export interface ResolvedCoworkDocumentCapabilities {
  readonly source: "legacy" | "server";
  readonly review: boolean;
  readonly provenance: boolean;
  readonly chat: boolean;
  readonly truth: boolean;
  readonly truthActivation: CoworkTruthActivation | null;
  readonly truthReadOnly: boolean;
  readonly includeTruthProjection: boolean;
}

const LEGACY_CAPABILITIES: ResolvedCoworkDocumentCapabilities = Object.freeze({
  source: "legacy",
  review: true,
  provenance: true,
  chat: true,
  truth: true,
  truthActivation: "enabled",
  truthReadOnly: false,
  includeTruthProjection: true,
});

export const resolveCoworkDocumentCapabilities = (
  document: Pick<CoworkDocumentSummary, "capabilities">,
): ResolvedCoworkDocumentCapabilities => {
  const envelope = document.capabilities;
  if (envelope === undefined) return LEGACY_CAPABILITIES;
  const activation = envelope.truth.activation;
  const truth =
    envelope.modules.truth &&
    (activation === "enabled" || activation === "paused");
  return {
    source: "server",
    review: envelope.modules.review,
    provenance: envelope.modules.provenance,
    chat: envelope.modules.chat,
    truth,
    truthActivation: activation,
    truthReadOnly: activation === "paused",
    includeTruthProjection: truth,
  };
};

export const coworkRailTabsForCapabilities = (
  capabilities: ResolvedCoworkDocumentCapabilities,
): readonly RailTab[] => [
  ...(capabilities.review ? (["review"] as const) : []),
  ...(capabilities.provenance ? (["provenance"] as const) : []),
  ...(capabilities.truth ? (["truth"] as const) : []),
  ...(capabilities.chat ? (["chat"] as const) : []),
];
