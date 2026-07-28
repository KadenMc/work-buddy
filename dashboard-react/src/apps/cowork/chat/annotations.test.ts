import { describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../../../widget-library/chat";
import {
  CoworkChatAnnotations,
  resolveSpanLink,
  resolveSpanLinks,
} from "./annotations";
import type { FeedbackCapture } from "./contracts";

const userMessage = (id: string, content: string): ChatMessage => ({
  id,
  author: "user",
  content,
});

const capture = (
  overrides: Partial<FeedbackCapture> & Pick<FeedbackCapture, "text">,
): FeedbackCapture => ({
  documentId: overrides.documentId ?? "doc-1",
  storeId: overrides.storeId ?? "store-1",
  evidenceId: overrides.evidenceId ?? `ev-${overrides.text}`,
  spanId: overrides.spanId ?? `span-${overrides.text}`,
  conversationId: overrides.conversationId ?? "c1",
  messageId: overrides.messageId ?? `message-${overrides.evidenceId ?? overrides.text}`,
  text: overrides.text,
  anchor: overrides.anchor,
});

describe("resolveSpanLinks", () => {
  it("links a feedback capture onto its exact user message id", () => {
    const messages: ChatMessage[] = [
      { id: "a1", author: "assistant", content: "I proposed some edits." },
      userMessage("u1", "this claim is too strong"),
    ];
    const links = resolveSpanLinks(messages, [
      capture({
        text: "this claim is too strong",
        spanId: "span-9",
        evidenceId: "ev-9",
        messageId: "u1",
      }),
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
    const links = resolveSpanLinks(messages, [
      capture({ text: "echoed text", messageId: "a1" }),
    ]);
    expect(links.size).toBe(0);
  });

  it("assigns repeated identical feedback by id despite out-of-order responses", () => {
    const messages: ChatMessage[] = [
      userMessage("u1", "same note"),
      userMessage("u2", "same note"),
    ];
    const links = resolveSpanLinks(messages, [
      capture({
        text: "same note",
        evidenceId: "ev-b",
        spanId: "span-b",
        messageId: "u2",
      }),
      capture({
        text: "same note",
        evidenceId: "ev-a",
        spanId: "span-a",
        messageId: "u1",
      }),
    ]);

    expect(links.get("u1")?.evidenceId).toBe("ev-a");
    expect(links.get("u2")?.evidenceId).toBe("ev-b");
  });

  it("carries the anchor through to the scroll-to target", () => {
    const messages: ChatMessage[] = [userMessage("u1", "fix this")];
    const links = resolveSpanLinks(messages, [
      capture({
        text: "fix this",
        messageId: "u1",
        anchor: { exact: "the passage", prefix: "before ", suffix: " after" },
      }),
    ]);
    expect(links.get("u1")?.target.anchor?.exact).toBe("the passage");
  });

  it("resolves repeated feedback by exact message id", () => {
    const feedback = [
      capture({
        text: "same note",
        evidenceId: "evidence-1",
        spanId: "span-1",
        messageId: "message-1",
        anchor: { exact: "first passage" },
      }),
      capture({
        text: "same note",
        evidenceId: "evidence-2",
        spanId: "span-2",
        messageId: "message-2",
        anchor: { exact: "second passage" },
      }),
    ];

    expect(
      resolveSpanLink(userMessage("message-2", "same note"), feedback)?.target,
    ).toEqual({
      spanId: "span-2",
      anchor: { exact: "second passage" },
    });
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

  it("replaces hydrated feedback so redacted links do not stay stale", () => {
    const store = new CoworkChatAnnotations();
    store.annotateFeedback(
      capture({
        text: "old note",
        evidenceId: "ev-old",
        messageId: "message-old",
      }),
    );

    store.replaceFeedback([
      capture({
        text: "current note",
        evidenceId: "ev-current",
        messageId: "message-current",
      }),
    ]);
    expect(store.getSnapshot().feedback.map((entry) => entry.evidenceId)).toEqual([
      "ev-current",
    ]);

    store.replaceFeedback([]);
    expect(store.getSnapshot().feedback).toHaveLength(0);
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
