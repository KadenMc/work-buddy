/**
 * The Chat-tab provider seam. The Chat tab reuses the house conversation machinery through
 * the ChatConversationProvider seam, and the live document-conversation provider is built in
 * chat/ over the house conversation HTTP surface (GET /api/conversations/<id>,
 * POST .../respond, the 3s poll). That provider is authored in parallel, so this seam is
 * coded against the demo provider's interface today and swaps to the live export in one line
 * once chat/ exports it.
 *
 * LIVE-CHAT WIRING SEAM: when chat/ ships its live document-conversation provider, import it
 * here and return it on the non-fixture path in place of createDemoChatProvider. The whole
 * surface reaches the live conversation through this one function, so no other file changes.
 */

import type { ChatConversationProvider } from "../../../widget-library/chat";
import { createDemoChatProvider } from "../rail/chatFixture";

export interface CoworkChatProviderOptions {
  readonly conversationId: string;
  /** True in demo / widget-lab / test mode, so the fixture provider is used deliberately. */
  readonly fixture: boolean;
}

/**
 * Resolve the Chat-tab provider for one document conversation. The demo provider backs the
 * fixture path and, until the live provider export exists in chat/, the live path too.
 */
export const resolveCoworkChatProvider = (
  options: CoworkChatProviderOptions,
): ChatConversationProvider => {
  // TODO: return chat/'s live document-conversation provider on the non-fixture path once it
  // exists. Coded against the demo provider's interface until then (see the seam note above).
  return createDemoChatProvider(options.conversationId);
};
