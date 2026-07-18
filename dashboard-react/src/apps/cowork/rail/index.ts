/**
 * Public surface of the Co-work Review rail. The orchestrator mounts CoworkRail
 * into the view frame in place of the rail placeholder, wiring the review and
 * chat providers, and optionally the anchor-rect seam for true stream
 * alignment. The in-memory providers back the default rendering and the tests.
 */

export { CoworkRail, type CoworkRailProps } from "./CoworkRail";
export { ReviewPanel, type ReviewPanelProps } from "./ReviewPanel";
export { RailDriftStrip, type RailDriftStripProps } from "./RailDriftStrip";
export { StreamView, type StreamViewProps } from "./StreamView";
export {
  QueueView,
  DEFAULT_QUEUE_BINDINGS,
  type QueueViewProps,
  type QueueBindings,
} from "./QueueView";
export { FilterLens, type FilterLensProps, type FilterCounts } from "./FilterLens";
export { MarkBar, type MarkBarProps, type MarkBarTarget } from "./MarkBar";
export { ProposalCard, type ProposalCardProps } from "./ProposalCard";
export { ClaimCard, type ClaimCardProps } from "./ClaimCard";
export { Inspector, type InspectorProps } from "./Inspector";

export { RailStore, isDirty } from "./store";
export type {
  RailState,
  RailTab,
  RailMode,
  RailFilter,
  RailSelectionKind,
} from "./store";
export { useRailState, shallowArrayEqual } from "./useRailState";
export { useReviewData, type UseReviewDataResult } from "./useReviewData";
export { useIsNarrow } from "./useIsNarrow";

export {
  InMemoryReviewProvider,
  demoReviewData,
  type InMemoryReviewSeed,
} from "./InMemoryReviewProvider";
export { createDemoChatProvider } from "./chatFixture";

export type {
  ReviewRailProvider,
  AnchorRectSource,
  SittingSubmission,
  ReviewInvalidationListener,
  ReviewUnsubscribe,
} from "./provider";

export {
  computeAlignedLayout,
  placementsEqual,
  type AlignInput,
  type AlignPlacement,
  type AlignOptions,
} from "./geometry";
export {
  useAlignedStream,
  type UseAlignedStreamOptions,
  type AlignedStreamController,
} from "./useAlignedStream";

export {
  useDraftPersistence,
  useUnsavedChangesGuard,
  loadDraft,
  saveDraft,
  clearDraft,
  draftStorageKey,
} from "./dirty";

export {
  orderedItems,
  visibleItems,
  filterCounts,
  matchesFilter,
  groupOf,
  type RailItem,
  type RailGroup,
} from "./items";

export {
  EDIT_VERBS,
  FLAG_VERBS,
  CLAIM_VERBS,
  PROPOSAL_VERB_LABEL,
  CLAIM_VERB_LABEL,
  verbsForProposal,
  isVerbDecidable,
  rejectAsFalseNeedsNegation,
  type VerbOption,
  type VerbTone,
  type VerbInput,
} from "./verbs";

export type {
  ReviewRailData,
  ReviewProposal,
  ReviewClaim,
  ReviewExpression,
  ProvenanceSpan,
  RailDriftHealth,
  StagedDecision,
  StagedClaimDecision,
  SittingResult,
  SittingItemResult,
  SittingResultKind,
  ProposalVerbKind,
  ClaimVerbKind,
  ProposalKind,
  ProposalChangeType,
  CoworkEpistemicState,
} from "./contracts";
