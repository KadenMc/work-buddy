import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import { ChatComposer } from "./ChatComposer";

describe("ChatComposer", () => {
  it("enables Send only with content and submits the trimmed value", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(<ChatComposer onSend={onSend} />);

    const input = screen.getByRole("textbox", { name: "Message" });
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();

    await userEvent.type(input, "  hello world  ");
    expect(send).toBeEnabled();
    await userEvent.click(send);

    await waitFor(() => expect(onSend).toHaveBeenCalledWith("hello world"));
    await waitFor(() => expect(input).toHaveValue(""));
  });

  it("submits on Enter and inserts a newline on Shift+Enter", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(<ChatComposer onSend={onSend} />);
    const input = screen.getByRole("textbox", { name: "Message" });

    await userEvent.type(input, "first{Shift>}{Enter}{/Shift}second");
    expect(onSend).not.toHaveBeenCalled();
    expect(input).toHaveValue("first\nsecond");

    await userEvent.type(input, "{Enter}");
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("first\nsecond"));
  });

  it("retains the draft when the send fails", async () => {
    const onSend = vi.fn().mockRejectedValue(new Error("offline"));
    render(<ChatComposer onSend={onSend} />);
    const input = screen.getByRole("textbox", { name: "Message" });

    await userEvent.type(input, "keep me");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(input).toHaveValue("keep me");
  });

  it("disables input and Send when the composer is disabled", () => {
    render(<ChatComposer onSend={vi.fn()} disabled />);
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("shows a pending state while a send is in flight", () => {
    render(<ChatComposer onSend={vi.fn()} sending />);
    expect(screen.getByText("Sending message")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sending message/ })).toBeDisabled();
  });

  it("surfaces the inline send error and stays accessible", async () => {
    const { container } = render(
      <ChatComposer onSend={vi.fn()} errorMessage="Message could not be delivered" />,
    );
    expect(
      screen.getByText("Message could not be delivered"),
    ).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });
});
