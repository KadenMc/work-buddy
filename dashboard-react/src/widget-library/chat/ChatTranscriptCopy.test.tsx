import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DashboardHelpProvider } from "../../dashboard/help";
import type { ChatMessage } from "./contracts";
import { ChatCopyAction, formatChatTranscript } from "./ChatTranscriptCopy";

const messages: readonly ChatMessage[] = [
  { id: "m1", author: "user", content: "Can you help?" },
  {
    id: "m2",
    author: "assistant",
    authorLabel: "Draft assistant",
    content: "Yes.\nHere is a second line.",
  },
  { id: "m3", author: "system", content: "Session paused." },
];

async function withClipboard(
  writeText: (value: string) => Promise<void>,
  test: () => Promise<void>,
): Promise<void> {
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  try {
    await test();
  } finally {
    if (original === undefined) {
      Reflect.deleteProperty(navigator, "clipboard");
    } else {
      Object.defineProperty(navigator, "clipboard", original);
    }
  }
}

describe("ChatCopyAction", () => {
  it("formats canonical speaker labels with spaces and blank lines", () => {
    expect(formatChatTranscript(messages)).toBe(
      "You: Can you help?\n\n" +
        "Draft assistant: Yes.\nHere is a second line.\n\n" +
        "System: Session paused.",
    );
  });

  it("copies only the canonical plain-text transcript and announces success", async () => {
    const writeText = vi.fn(async () => undefined);
    await withClipboard(writeText, async () => {
      render(<ChatCopyAction messages={messages} />);
      await userEvent.click(screen.getByRole("button", { name: "Copy chat" }));

      expect(writeText).toHaveBeenCalledWith(formatChatTranscript(messages));
      expect(await screen.findByRole("status")).toHaveTextContent(
        "Chat copied to the clipboard.",
      );
      expect(screen.getByRole("button", { name: "Copy chat" })).toBeEnabled();
    });
  });

  it("keeps raw clipboard errors private and announces a safe fallback", async () => {
    const writeText = vi.fn(async () => {
      throw new Error("private clipboard diagnostic");
    });
    await withClipboard(writeText, async () => {
      render(<ChatCopyAction messages={messages} />);
      await userEvent.click(screen.getByRole("button", { name: "Copy chat" }));

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(
        "Chat could not be copied. Select the messages and copy them manually.",
      );
      expect(alert).not.toHaveTextContent("private clipboard diagnostic");
    });
  });

  it("exposes optional detail through keyboard Hover Help", async () => {
    const user = userEvent.setup();
    render(
      <DashboardHelpProvider enabled>
        <ChatCopyAction messages={messages} />
      </DashboardHelpProvider>,
    );

    await user.tab();
    const copy = screen.getByRole("button", { name: "Copy chat" });
    expect(copy).toHaveFocus();
    expect(copy).not.toHaveAttribute("title");
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("Copy this conversation.");
    expect(tooltip).toHaveTextContent(
      "Timestamps, model details and interface controls are not included.",
    );
  });
});
