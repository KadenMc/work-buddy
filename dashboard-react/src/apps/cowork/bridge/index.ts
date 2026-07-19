/**
 * The live review bridge. It turns the Co-work surface from demo-backed to ledger-backed:
 * proposals flow R2 -> editor marks + rail cards -> sitting -> R5. The surface consumes
 * useCoworkBridge in live mode and the demo providers behind the fixture switch.
 */

export {
  useCoworkBridge,
  DEFAULT_BRIDGE_SEED_MARKDOWN,
  type UseCoworkBridgeOptions,
  type CoworkBridge,
  type CoworkBridgeEditorMountProps,
  type CoworkLiveHealth,
} from "./useCoworkBridge";
export {
  CoworkBridgeEditor,
  type CoworkBridgeEditorProps,
  type CoworkEditorReadyContext,
} from "./CoworkBridgeEditor";
export {
  LiveReviewRailProvider,
  type LiveReviewRailProviderOptions,
  type ProposalsListener,
  type ReviewDataListener,
} from "./LiveReviewRailProvider";
export {
  HttpCoworkDocClient,
  type CoworkDocClient,
  type HttpCoworkDocClientOptions,
} from "./HttpCoworkDocClient";
export {
  DomAnchorRectSource,
  type DomAnchorRectSourceOptions,
} from "./DomAnchorRectSource";
export { ProposalIngestor } from "./proposalIngestor";
export {
  submitCoworkSitting,
  toDecisionItem,
  toRailSittingResult,
  type SubmitCoworkSittingParams,
} from "./sittingSubmit";
export { createEditorMaterializeRenderer } from "./materialize";
export {
  resolveCoworkChatProvider,
  type CoworkChatProviderOptions,
} from "./chatProvider";
export {
  mapR2ToReview,
  mapProposal,
  mapProposalInput,
  deriveChangeType,
  deriveAnchorLabel,
  type MappedReview,
} from "./reviewMapping";
export type {
  R2DocPayload,
  R2Proposal,
  R2QuoteAnchor,
  R2Producer,
  R2Expression,
  R2ProvenanceSpan,
  R2Hashes,
  R2Drift,
  R2ClaimRef,
} from "./types";
