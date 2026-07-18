/**
 * Public surface of the Co-work feedback affordance (PRD job 5). The live editor
 * host mounts CoworkFeedbackAffordance, which captures a document selection as a
 * quote anchor, POSTs the R9 feedback route, and hands the capture up so the
 * surface annotates the Chat tab and switches to it. Everything downstream (the
 * annotations store, the Chat span-link, the document conversation) already
 * exists, so this module is the entry point only.
 */

export {
  CoworkFeedbackAffordance,
  type CoworkFeedbackAffordanceProps,
} from "./CoworkFeedbackAffordance";

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
