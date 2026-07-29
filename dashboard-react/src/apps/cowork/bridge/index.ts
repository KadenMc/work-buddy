/**
 * The live review bridge. It turns the Co-work surface from demo-backed to ledger-backed:
 * one R2 snapshot feeds editor decorations and rail cards, while accepted sitting
 * decisions materialize on an isolated canonical clone before R5. The surface consumes
 * useCoworkBridge in live mode and demo providers behind the fixture switch.
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
  type VerifyRecheckRequest,
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
export {
  submitCoworkSitting,
  toDecisionItem,
  toRailSittingResult,
  type SubmitCoworkSittingParams,
} from "./sittingSubmit";
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
