// Co-work Chat tab types that extend the house chat primitives with the two
// document-scoped concerns the plain conversation surface does not carry: a
// feedback message anchored to a document span, and the delivery status of a
// routing note the human sent to the document agent. The transport itself is
// the house conversation HTTP surface, so nothing here re-declares a message
// shape. It only adds the document linkage the Co-work surface renders on top
// of the shared ChatMessage.

import type { CoworkDocumentAgent } from "./documentConversationBinding";
import type { ChatExecutionSnapshot } from "../../../widget-library/chat";

/**
 * A quote anchor as the R9 feedback route and kernel anchors.py address it. The
 * exact quote plus its neighbourhood re-locate the passage after edits, so the
 * scroll-to seam receives it rather than an ephemeral node id.
 */
export interface QuoteAnchor {
  readonly exact: string;
  readonly prefix?: string;
  readonly suffix?: string;
  readonly nodeIdHint?: string | null;
}

/**
 * The R9 feedback-capture response, plus the verbatim text and the span anchor
 * the caller sent. R9 posts the text as the user's message and returns the
 * evidence, span, message, and conversation it landed in. The exact message id
 * is authoritative, so repeated identical feedback remains unambiguous even
 * when responses arrive out of order.
 */
export interface FeedbackCapture {
  /** The document identity that originated the async feedback request. */
  readonly documentId: string;
  readonly storeId: string;
  readonly evidenceId: string;
  readonly spanId: string;
  readonly conversationId: string;
  /** The exact user message created by R9. */
  readonly messageId: string;
  /** Agent state returned by R9 after its user-authorized ensure attempt. */
  readonly agent?: CoworkDocumentAgent;
  /**
   * The exact execution selection/revision returned by the same R9
   * transaction. Present on live captures so a first feedback turn cannot
   * leave the picker holding the earlier unpinned revision.
   */
  readonly execution?: ChatExecutionSnapshot;
  /** The verbatim feedback text, exactly as R9 posted it as the user message. */
  readonly text: string;
  /** The document span the feedback was anchored to, for the scroll-to seam. */
  readonly anchor?: QuoteAnchor;
}

/**
 * The document passage a chat item scrolls to. Handed to the callback prop, so
 * the chat surface never imports the editor or the rail's proposal-keyed
 * ReviewAnchorController directly (the feedback anchor is span-keyed, not proposal
 * keyed).
 */
export interface ScrollAnchorTarget {
  readonly spanId: string;
  readonly anchor?: QuoteAnchor;
}

/** Delivery outcome of one routing note (a redirect or endorse) to the agent. */
export type RoutingDeliveryState = "delivered" | "queued" | "failed";

/**
 * The record the submit path hands the chat side after a redirect or endorse
 * kept a proposal open and routed the human's guidance into this conversation.
 * The verb is the shipped gesture-kind name. Delivery status comes from the
 * server's conversation write and document-agent outcome; it is never inferred
 * from the review gesture alone.
 */
export interface RoutingDeliveryInput {
  readonly verb: "redirect" | "endorse";
  readonly proposalId: string;
  readonly state: RoutingDeliveryState;
  /** The human's verbatim note on a redirect. Endorse carries none. */
  readonly note?: string;
  /** A short, user-safe reason when persistence or agent startup was incomplete. */
  readonly reason?: string;
  /** Server-issued document conversation that received the routing note. */
  readonly conversationId?: string;
  /** Agent state returned by the same sitting transaction. */
  readonly agent?: CoworkDocumentAgent;
  /** Exact execution choice/revision returned by the same transaction. */
  readonly execution?: ChatExecutionSnapshot;
}

/** A stored routing delivery: an input plus the store-assigned notice id. */
export interface RoutingDelivery extends RoutingDeliveryInput {
  readonly id: string;
}

/** A span link resolved onto exactly one transcript message. */
export interface ResolvedSpanLink {
  readonly messageId: string;
  readonly evidenceId: string;
  readonly target: ScrollAnchorTarget;
}
