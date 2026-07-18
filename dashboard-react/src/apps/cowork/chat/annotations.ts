// The document linkage layer beside the live transport. The house conversation
// store carries only role and text on a message, so the two Co-work concerns
// that need more (a feedback message anchored to a document span, a routing note
// delivered to the document agent) live here as a small observable store the
// chat panel reads through useSyncExternalStore. The transport stays pure of
// document knowledge, and this store stays pure of I/O.

import type { ChatMessage } from "../../../widget-library/chat";
import type {
  FeedbackCapture,
  ResolvedSpanLink,
  RoutingDelivery,
  RoutingDeliveryInput,
} from "./contracts";

/** An immutable point-in-time view of the linkage layer. */
export interface CoworkChatAnnotationsSnapshot {
  readonly feedback: readonly FeedbackCapture[];
  readonly routing: readonly RoutingDelivery[];
}

type Listener = () => void;

const EMPTY_SNAPSHOT: CoworkChatAnnotationsSnapshot = {
  feedback: [],
  routing: [],
};

/**
 * Resolve feedback span links onto transcript messages. R9 posts the feedback
 * verbatim as the user's message, so each capture matches the first not-yet
 * linked user message whose content equals its text, in registration order.
 * Order-preserving so repeated identical feedback lines each claim a distinct
 * message rather than colliding on the first.
 */
export function resolveSpanLinks(
  messages: readonly ChatMessage[],
  feedback: readonly FeedbackCapture[],
): Map<string, ResolvedSpanLink> {
  const links = new Map<string, ResolvedSpanLink>();
  const consumed = new Set<string>();
  for (const capture of feedback) {
    const match = messages.find(
      (message) =>
        message.author === "user" &&
        message.content === capture.text &&
        !consumed.has(message.id),
    );
    if (match === undefined) continue;
    consumed.add(match.id);
    links.set(match.id, {
      messageId: match.id,
      evidenceId: capture.evidenceId,
      target: { spanId: capture.spanId, anchor: capture.anchor },
    });
  }
  return links;
}

/**
 * The observable linkage store. Feedback annotations are idempotent by evidence
 * id (a re-notify after a poll does not duplicate a span link), and routing
 * deliveries append with a store-assigned notice id.
 */
export class CoworkChatAnnotations {
  private feedbackList: FeedbackCapture[] = [];
  private routingList: RoutingDelivery[] = [];
  private readonly listeners = new Set<Listener>();
  private sequence = 0;
  private cached: CoworkChatAnnotationsSnapshot = EMPTY_SNAPSHOT;

  /** Record the R9 feedback response so its message renders the span linkage. */
  annotateFeedback(capture: FeedbackCapture): void {
    if (this.feedbackList.some((entry) => entry.evidenceId === capture.evidenceId)) {
      return;
    }
    this.feedbackList = [...this.feedbackList, capture];
    this.recompute();
    this.emit();
  }

  /** Record a routing-note delivery (redirect or endorse) and its outcome. */
  annotateRoutingDelivery(input: RoutingDeliveryInput): RoutingDelivery {
    this.sequence += 1;
    const entry: RoutingDelivery = { ...input, id: `routing-${this.sequence}` };
    this.routingList = [...this.routingList, entry];
    this.recompute();
    this.emit();
    return entry;
  }

  /** Drop a routing delivery notice by its store-assigned id, if present. */
  dismissRoutingDelivery(id: string): void {
    const next = this.routingList.filter((entry) => entry.id !== id);
    if (next.length === this.routingList.length) return;
    this.routingList = next;
    this.recompute();
    this.emit();
  }

  /** Stable snapshot getter for useSyncExternalStore. */
  getSnapshot = (): CoworkChatAnnotationsSnapshot => this.cached;

  /** Stable subscribe for useSyncExternalStore. */
  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private recompute(): void {
    this.cached = {
      feedback: this.feedbackList,
      routing: this.routingList,
    };
  }

  private emit(): void {
    for (const listener of [...this.listeners]) listener();
  }
}
