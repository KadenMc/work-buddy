/** Passage requests capture an exact quote and a verbatim note for document Chat. */
export { CoworkPassageRequest, type CoworkPassageRequestProps } from "./CoworkPassageRequest";

export {
  HttpCoworkFeedbackTransport,
  InMemoryCoworkFeedbackTransport,
  type CoworkFeedbackTransport,
  type CoworkFeedbackRequest,
  type CoworkFeedbackResponse,
  type CoworkFeedbackSpan,
} from "./feedbackClient";

export {
  DEFAULT_FEEDBACK_CONTEXT_CHARS,
  quoteAnchorFromRange,
  type RangeQuoteAnchor,
} from "./feedbackAnchor";
