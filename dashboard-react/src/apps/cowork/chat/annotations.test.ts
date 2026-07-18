import { describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../../../widget-library/chat";
import { CoworkChatAnnotations, resolveSpanLinks } from "./annotations";
import type { FeedbackCapture } from "./contracts";

const userMessage = (id: string, content: string): ChatMessage => ({
  id,
  author: "user",
  content,
});

const capture = (
  overrides: Partial<FeedbackCapture> & Pick<FeedbackCapture, "text">,
): FeedbackCapture => ({
  evidenceId: overrides.evidenceId ?? `ev-${overrides.text}`,
  spanId: overrides.spanId ?? `span-${overrides.text}`,
  conversationId: overrides.conversationId ?? "c1",
  text: overrides.text,
  anchor: overrides.anchor,
});

describe("resolveSpanLinks", () => {
  it("links a feedback capture onto the user message with its verbatim text", () => {
    const messages: ChatMessage[] = [
      { id: "a1", author: "assistant", content: "I proposed some edits." },
      userMessage("u1", "this claim is too strong"),
    ];
    const links = resolveSpanLinks(messages, [
      capture({ text: "this claim is too strong", spanId: "span-9", evidenceId: "ev-9" }),
    ]);

    expect(links.get("u1")).toMatchObject({
      messageId: "u1",
      evidenceId: "ev-9",
      target: { spanId: "span-9" },
    });
  });

  it("does not link an assistant message even on a text match", () => {
    const messages: ChatMessage[] = [
      { id: "a1", author: "assistant", content: "echoed text" },
    ];
    const links = resolveSpanLinks(messages, [capture({ text: "echoed text" })]);
    expect(links.size).toBe(0);
  });

  it("assigns distinct messages to repeated identical feedback in order", () => {
    const messages: ChatMessage[] = [
      userMessage("u1", "same note"),
      userMessage("u2", "same note"),
    ];
    const links = resolveSpanLinks(messages, [
      capture({ text: "same note", evidenceId: "ev-a", spanId: "span-a" }),
      capture({ text: "same note", evidenceId: "ev-b", spanId: "span-b" }),
    ]);

    expect(links.get("u1")?.evidenceId).toBe("ev-a");
    expect(links.get("u2")?.evidenceId).toBe("ev-b");
  });

  it("carries the anchor through to the scroll-to target", () => {
    const messages: ChatMessage[] = [userMessage("u1", "fix this")];
    const links = resolveSpanLinks(messages, [
      capture({
        text: "fix this",
        anchor: { exact: "the passage", prefix: "before ", suffix: " after" },
      }),
    ]);
    expect(links.get("u1")?.target.anchor?.exact).toBe("the passage");
  });
});

describe("CoworkChatAnnotations", () => {
  it("records feedback idempotently by evidence id and notifies", () => {
    const store = new CoworkChatAnnotations();
    const listener = vi.fn();
    store.subscribe(listener);

    store.annotateFeedback(capture({ text: "note", evidenceId: "ev-1" }));
    store.annotateFeedback(capture({ text: "note", evidenceId: "ev-1" }));

    expect(store.getSnapshot().feedback).toHaveLength(1);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("appends routing deliveries with a stable id and notifies", () => {
    const store = new CoworkChatAnnotations();
    const listener = vi.fn();
    store.subscribe(listener);

    const delivery = store.annotateRoutingDelivery({
      verb: "redirect",
      proposalId: "p1",
      state: "delivered",
      note: "tighten the scope",
    });

    expect(delivery.id).toMatch(/^routing-/);
    expect(store.getSnapshot().routing).toHaveLength(1);
    expect(store.getSnapshot().routing[0]).toMatchObject({
      verb: "redirect",
      proposalId: "p1",
      state: "delivered",
    });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("dismisses a routing delivery by id", () => {
    const store = new CoworkChatAnnotations();
    const delivery = store.annotateRoutingDelivery({
      verb: "endorse",
      proposalId: "p2",
      state: "delivered",
    });
    store.dismissRoutingDelivery(delivery.id);
    expect(store.getSnapshot().routing).toHaveLength(0);
  });

  it("returns a referentially stable snapshot until a mutation", () => {
    const store = new CoworkChatAnnotations();
    const before = store.getSnapshot();
    expect(store.getSnapshot()).toBe(before);
    store.annotateFeedback(capture({ text: "note" }));
    expect(store.getSnapshot()).not.toBe(before);
  });

  it("stops notifying after unsubscribe", () => {
    const store = new CoworkChatAnnotations();
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);
    unsubscribe();
    store.annotateFeedback(capture({ text: "note" }));
    expect(listener).not.toHaveBeenCalled();
  });
});
