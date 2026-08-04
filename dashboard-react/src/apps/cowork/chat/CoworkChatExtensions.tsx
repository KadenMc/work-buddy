import { Button, InlineAlert } from "../../../ui";
import type {
  ChatActionSnapshotContext,
  ChatAuthorRole,
} from "../../../widget-library/chat";
import type {
  ResolvedSpanLink,
  RoutingDelivery,
  ScrollAnchorTarget,
} from "./contracts";

export interface CoworkPassageActionProps {
  readonly link: ResolvedSpanLink;
  readonly onActivate: (target: ScrollAnchorTarget) => void;
}

/**
 * Co-work's document-specific message accessory. The shared chat surface owns
 * the message bubble and invokes this accessory only for the exact canonical
 * message selected by the annotation layer.
 */
export function CoworkPassageAction({
  link,
  onActivate,
}: CoworkPassageActionProps) {
  const quote = (link.target.anchor?.exact ?? "").replace(/\s+/g, " ").trim();
  const excerpt =
    quote.length <= 96 ? quote : `${quote.slice(0, 95).trimEnd()}…`;
  const accessibleName =
    excerpt.length > 0
      ? `Jump to passage: "${excerpt}"`
      : "Jump to the anchored passage";

  return (
    <div className="wb-cowork-chat-msg__anchor">
      <Button
        variant="secondary"
        size="small"
        className="wb-cowork-chat-anchor-button"
        onClick={() => onActivate(link.target)}
        aria-label={accessibleName}
      >
        Jump to passage
      </Button>
    </div>
  );
}

export function CoworkActionSnapshotProvenance({
  context,
  author,
}: {
  readonly context: ChatActionSnapshotContext;
  readonly author: ChatAuthorRole;
}) {
  const words =
    context.targetWordCount === undefined
      ? ""
      : ` · ${context.targetWordCount.toLocaleString()} words`;
  let prefix = "Working on";
  if (
    author === "assistant" &&
    context.consumption?.fetchOutcome === "unavailable"
  ) {
    prefix =
      context.discussion !== undefined
        ? "Couldn’t open Co-think context"
        : "Couldn’t open Working on";
  } else if (context.discussion !== undefined) {
    prefix = "Discussing Co-think";
  } else if (author === "assistant" && context.consumption !== undefined) {
    prefix = "Used Working on";
  }
  return (
    <div
      className="wb-cowork-chat-msg__context"
      aria-label={`${prefix}: ${context.targetLabel}`}
    >
      <span className="wb-cowork-chat-msg__context-label">
        {prefix}: {context.targetLabel}
        {words}
      </span>
    </div>
  );
}

function routingLabel(delivery: RoutingDelivery): string {
  const target =
    delivery.verb === "redirect" ? "Redirect" : "Endorsement";
  if (delivery.state === "delivered") {
    return `${target} sent to the document agent.`;
  }
  if (delivery.state === "queued") {
    return `${target} saved in chat and waiting for delivery.`;
  }
  return `${target} could not be saved in chat.`;
}

export interface CoworkRoutingNoticesProps {
  readonly deliveries: readonly RoutingDelivery[];
  readonly onDismiss?: (id: string) => void;
}

/**
 * Dismissible delivery receipts appended to the shared transcript. They remain
 * auxiliary Co-work UI and never masquerade as durable conversation messages.
 * The surrounding transcript log announces additions, so these notices do not
 * create a competing nested live region.
 */
export function CoworkRoutingNotices({
  deliveries,
  onDismiss,
}: CoworkRoutingNoticesProps) {
  if (deliveries.length === 0) return null;

  return (
    <ul className="wb-cowork-chat-routing" aria-label="Routing notes">
      {deliveries.map((delivery) => (
        <li
          key={delivery.id}
          className="wb-cowork-chat-routing__item"
          data-state={delivery.state}
        >
          <InlineAlert
            tone={delivery.state === "failed" ? "danger" : "info"}
            className="wb-cowork-chat-routing__alert"
          >
            <span aria-hidden="true">
              {delivery.state === "failed" ? "! " : "→ "}
            </span>
            {routingLabel(delivery)}
            {delivery.reason !== undefined && delivery.reason.length > 0 ? (
              <span className="wb-cowork-chat-routing__reason">
                {" "}
                {delivery.reason}
              </span>
            ) : null}
          </InlineAlert>
          {onDismiss !== undefined ? (
            <Button
              variant="secondary"
              size="small"
              className="wb-cowork-chat-routing__dismiss"
              onClick={() => onDismiss(delivery.id)}
              aria-label={`Dismiss ${delivery.state} ${delivery.verb} notice`}
            >
              <span aria-hidden="true">Dismiss</span>
            </Button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
