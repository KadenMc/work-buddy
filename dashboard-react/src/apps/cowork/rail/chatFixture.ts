/**
 * A demo chat provider for the Co-work Chat tab. In-memory for now, a live
 * conversation transport (the house conversation_* seam) implements the same
 * interface. The agent replies in plain language, mirroring the feedback loop
 * the PRD describes.
 */

import { InMemoryChatProvider } from "../../../widget-library/chat";
import type { ChatMessage } from "../../../widget-library/chat";

const SEED_MESSAGES: readonly ChatMessage[] = [
  {
    id: "c1",
    author: "assistant",
    authorLabel: "Document agent",
    content:
      "I proposed a few tracked edits and one flag on this document. Open the Review tab to walk them.",
    createdAt: "2026-07-17T12:00:00Z",
  },
];

/** Build a scripted document-conversation provider for one document. */
export function createDemoChatProvider(
  conversationId: string,
): InMemoryChatProvider {
  return new InMemoryChatProvider({
    conversationId,
    title: "Document conversation",
    status: "open",
    agentLiveness: "alive",
    messages: SEED_MESSAGES,
    autoReply: (input, snapshot) => [
      {
        id: `reply-${snapshot.messages.length}`,
        author: "assistant",
        authorLabel: "Document agent",
        content: `Understood. I will turn "${input.value}" into a tracked-change proposal in Review.`,
        createdAt: new Date().toISOString(),
      },
    ],
  });
}
