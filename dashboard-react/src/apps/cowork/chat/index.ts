/**
 * Public surface of the Co-work Chat tab. The workspace surface mounts
 * CoworkChatPanel as a thin document-specific adapter around the reusable
 * ConversationChat surface. Feedback and sitting routing write document
 * linkage into CoworkChatAnnotations; the passage callback remains abstract so
 * this package never imports the editor.
 */

export {
  CoworkChatAnnotations,
  resolveSpanLink,
  resolveSpanLinks,
  type CoworkChatAnnotationsSnapshot,
} from "./annotations";

export {
  HttpCoworkDocumentConversationBindingClient,
  normalizeCoworkDocumentAgent,
  type CoworkDocumentAgent,
  type CoworkDocumentAgentStatus,
  type CoworkDocumentConversationBinding,
  type CoworkDocumentConversationBindingClient,
} from "./documentConversationBinding";

export {
  useDocumentConversationBinding,
  type CoworkConversationBindingPhase,
  type CoworkConversationBindingState,
  type UseDocumentConversationBindingResult,
} from "./useDocumentConversationBinding";

export {
  CoworkChatPanel,
  type CoworkChatPanelProps,
} from "./CoworkChatPanel";

export {
  CoworkPassageAction,
  CoworkRoutingNotices,
  type CoworkPassageActionProps,
  type CoworkRoutingNoticesProps,
} from "./CoworkChatExtensions";

export type {
  FeedbackCapture,
  QuoteAnchor,
  ResolvedSpanLink,
  RoutingDelivery,
  RoutingDeliveryInput,
  RoutingDeliveryState,
  ScrollAnchorTarget,
} from "./contracts";
