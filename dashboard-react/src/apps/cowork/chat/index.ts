/**
 * Public surface of the Co-work Chat tab. The workspace surface mounts
 * CoworkChatPanel in place of the demo chat and constructs
 * HttpChatConversationProvider as the live document-conversation transport. The
 * feedback entry point (from a document selection, via R9) and the submit path
 * (redirect and endorse routing) write the document linkage into a shared
 * CoworkChatAnnotations store the panel reads. The scroll-to-passage seam is a
 * callback prop, so nothing here imports the editor or the rail's
 * proposal-keyed AnchorRectSource.
 */

export {
  HttpChatConversationProvider,
  createHttpChatProvider,
  type HttpChatConfig,
} from "./HttpChatConversationProvider";

export {
  CoworkChatAnnotations,
  resolveSpanLinks,
  type CoworkChatAnnotationsSnapshot,
} from "./annotations";

export {
  CoworkChatPanel,
  type CoworkChatPanelProps,
} from "./CoworkChatPanel";

export {
  CoworkChatTranscript,
  type CoworkChatTranscriptProps,
} from "./CoworkChatTranscript";

export type {
  FeedbackCapture,
  QuoteAnchor,
  ResolvedSpanLink,
  RoutingDelivery,
  RoutingDeliveryInput,
  RoutingDeliveryState,
  ScrollAnchorTarget,
} from "./contracts";
