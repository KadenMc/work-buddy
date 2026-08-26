import { Check } from "@phosphor-icons/react/Check";
import { Copy } from "@phosphor-icons/react/Copy";
import { useEffect, useState } from "react";

import { HelpTarget } from "../../dashboard/help";
import { IconButton } from "../../ui";
import type { ChatMessage } from "./contracts";

export function chatMessageAuthorLabel(message: ChatMessage): string {
  if (message.author === "user") return "You";
  if (message.author === "system") return message.authorLabel ?? "System";
  return message.authorLabel ?? "Assistant";
}

/** Canonical plain-text export: message content with explicit speaker labels. */
export function formatChatTranscript(messages: readonly ChatMessage[]): string {
  return messages
    .map((message) => `${chatMessageAuthorLabel(message)}: ${message.content}`)
    .join("\n\n");
}

type CopyStatus = "idle" | "copied" | "error";

export interface ChatCopyActionProps {
  readonly messages: readonly ChatMessage[];
}

/** Copy the canonical transcript without persisting it or feature accessories. */
export function ChatCopyAction({ messages }: ChatCopyActionProps) {
  const [status, setStatus] = useState<CopyStatus>("idle");

  useEffect(() => {
    if (status !== "copied") return undefined;
    const timer = window.setTimeout(() => setStatus("idle"), 2_500);
    return () => window.clearTimeout(timer);
  }, [status]);

  const copy = async () => {
    try {
      const writeText = globalThis.navigator?.clipboard?.writeText;
      if (writeText === undefined) throw new Error("Clipboard unavailable");
      await writeText.call(globalThis.navigator.clipboard, formatChatTranscript(messages));
      setStatus("copied");
    } catch {
      setStatus("error");
    }
  };

  const announcement =
    status === "copied"
      ? "Chat copied to the clipboard."
      : status === "error"
        ? "Chat could not be copied. Select the messages and copy them manually."
        : "";

  return (
    <>
      <HelpTarget
        content={{
          summary: "Copy this conversation.",
          details:
            "Copies each speaker label and message as plain text. Timestamps, model details and interface controls are not included.",
        }}
        placement="bottom end"
        reactAriaComposite
      >
        <IconButton
          label="Copy chat"
          title=""
          icon={status === "copied" ? <Check weight="bold" /> : <Copy weight="duotone" />}
          variant="ghost"
          size="small"
          onClick={() => void copy()}
        />
      </HelpTarget>
      {announcement ? (
        <span
          className="wb-visually-hidden"
          role={status === "error" ? "alert" : "status"}
        >
          {announcement}
        </span>
      ) : null}
    </>
  );
}
