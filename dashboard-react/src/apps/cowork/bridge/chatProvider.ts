/**
 * The Chat-tab provider seam. The Chat tab reuses the house conversation machinery through
 * the ChatConversationProvider seam, and the live document-conversation provider is built in
 * chat/ over the house conversation HTTP surface (GET /api/conversations/<id>,
 * POST .../respond, the 3s poll). The fixture path keeps the deterministic demo provider for
 * widget-lab and the tests, and the live path uses chat/'s createHttpChatProvider so the
 * whole surface reaches the live conversation through this one function.
 */

import type { ChatConversationProvider } from "../../../widget-library/chat";
import { createHttpChatProvider } from "../chat";
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
  if (options.fixture) {
    return createDemoChatProvider(options.conversationId);
  }
  return createHttpChatProvider({ conversationId: options.conversationId });
};
