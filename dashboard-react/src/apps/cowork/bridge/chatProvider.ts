/**
 * The Chat-tab provider seam. The Chat tab reuses the house conversation
 * machinery through ChatConversationProvider. The neutral dashboard adapter
 * owns the live HTTP transport; this module only chooses between that adapter
 * and Co-work's deterministic fixture.
 */

import { createHttpChatProvider } from "../../../dashboard/conversations";
import type { ChatConversationProvider } from "../../../widget-library/chat";
import { createDemoChatProvider } from "../rail/chatFixture";

export interface CoworkChatProviderOptions {
  readonly conversationId: string;
  /** True in demo / widget-lab / test mode, so the fixture provider is used deliberately. */
  readonly fixture: boolean;
}

/**
 * Resolve the Chat-tab provider for one document conversation.
 */
export const resolveCoworkChatProvider = (
  options: CoworkChatProviderOptions,
): ChatConversationProvider => {
  if (options.fixture) {
    return createDemoChatProvider(options.conversationId);
  }
  return createHttpChatProvider({ conversationId: options.conversationId });
};
