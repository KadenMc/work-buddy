import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DashboardHelpProvider } from "../../dashboard/help";
import { expectNoAccessibilityViolations } from "../../test/setup";
import { ChatComposer } from "./ChatComposer";
import chatStyles from "./styles.css?raw";
import type { ChatExecutionControl } from "./useChatExecutionProfile";

const changingExecution = (): ChatExecutionControl => ({
  snapshot: {
    selection: {
      providerId: "claude-code",
      modelId: "sonnet",
      providerLabel: "Claude Code",
      modelLabel: "Sonnet",
      revision: "execution:1",
    },
    providers: [
      {
        id: "claude-code",
        label: "Claude Code",
        available: true,
        models: [{ id: "sonnet", label: "Sonnet", available: true }],
      },
    ],
  },
  status: "ready",
  selecting: true,
  error: null,
  announcement: null,
  currentAvailable: true,
  select: vi.fn(async () => {}),
  retry: vi.fn(),
});

function mockTextareaGeometry(): void {
  const readComputedStyle = globalThis.getComputedStyle.bind(globalThis);
  vi.spyOn(globalThis, "getComputedStyle").mockImplementation((element, pseudo) => {
    const style = readComputedStyle(element, pseudo);
    if (!(element instanceof HTMLTextAreaElement)) return style;
    return new Proxy(style, {
      get(target, property) {
        if (property === "borderTopWidth" || property === "borderBottomWidth") {
          return "1px";
        }
        if (property === "maxHeight") return "160px";
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ChatComposer", () => {
  it("keeps the complete textarea focus ring inside clipped chat hosts and freezes waiting dots for reduced motion", () => {
    const focusRule = chatStyles.match(
      /\.wb-chat-composer__input:focus-visible\s*\{([^}]*)\}/,
    )?.[1];
    expect(focusRule).toContain(
      "outline-offset: calc(-1 * var(--wb-focus-width))",
    );
    expect(chatStyles).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.wb-chat-typing__dot\s*\{[\s\S]*?animation: none;[\s\S]*?opacity: 0\.65;/,
    );
  });

  it("offers an explicit host action without enabling or sending an empty draft", async () => {
    const onSend = vi.fn();
    const onAction = vi.fn();
    const onDraftChange = vi.fn();
    render(<ChatComposer onSend={onSend} disabled onDraftChange={onDraftChange} primaryAction={{ label: "Launch", disabled: false, onAction }} />);

    const launch = screen.getByRole("button", { name: "Launch" });
    expect(launch).toBeEnabled();
    expect(launch).toHaveAttribute("type", "button");
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    await userEvent.click(launch);
    await waitFor(() => expect(launch).toBeEnabled());
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onSend).not.toHaveBeenCalled();
    expect(onDraftChange).not.toHaveBeenCalled();
  });

  it("invokes the host synchronously once and locks the picker until its promise settles", async () => {
    let finish: (() => void) | undefined;
    const pending = new Promise<void>((resolve) => { finish = resolve; });
    const onAction = vi.fn(() => pending);
    const onSend = vi.fn();
    const onDraftChange = vi.fn();
    render(<ChatComposer onSend={onSend} initialValue="Not shared" onDraftChange={onDraftChange} execution={{ ...changingExecution(), selecting: false }} primaryAction={{ label: "Launch", pendingLabel: "Launching…", disabled: false, onAction }} />);
    const input = screen.getByRole("textbox", { name: "Message" });
    const launch = screen.getByRole("button", { name: "Launch" });

    act(() => {
      launch.click();
      expect(onAction).toHaveBeenCalledTimes(1);
      launch.click();
    });
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Launching…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Run with/ })).toBeDisabled();
    await userEvent.type(input, " while launching");
    await act(async () => { finish?.(); await pending; });

    expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Run with/ })).toBeEnabled();
    expect(input).toHaveValue("Not shared while launching");
    expect(onDraftChange).not.toHaveBeenCalledWith("");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("leaves errors and retry identity with the host without consuming the draft", async () => {
    const onAction = vi.fn().mockRejectedValueOnce(new Error("Launch failed")).mockResolvedValue(undefined);
    const onSend = vi.fn();
    const onDraftChange = vi.fn();
    const { rerender } = render(<ChatComposer onSend={onSend} initialValue="Keep this message" onDraftChange={onDraftChange} primaryAction={{ label: "Launch", disabled: false, onAction }} />);
    const input = screen.getByRole("textbox", { name: "Message" });
    await userEvent.click(screen.getByRole("button", { name: "Launch" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Launch" })).toBeEnabled());

    rerender(<ChatComposer onSend={onSend} onDraftChange={onDraftChange} accessory={<p role="alert">Launch failed. Retry the same attempt.</p>} primaryAction={{ label: "Retry Launch", disabled: false, onAction }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Launch failed. Retry the same attempt.");
    expect(input).toHaveValue("Keep this message");
    await userEvent.click(screen.getByRole("button", { name: "Retry Launch" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry Launch" })).toBeEnabled());
    expect(onAction).toHaveBeenCalledTimes(2);
    expect(input).toHaveValue("Keep this message");
    expect(onSend).not.toHaveBeenCalled();
    expect(onDraftChange).not.toHaveBeenCalled();
  });

  it("keeps textarea Enter as editing and authorizes only keyboard activation of the action", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    const onSend = vi.fn();
    render(<ChatComposer onSend={onSend} primaryAction={{ label: "Launch", disabled: false, onAction }} />);
    const input = screen.getByRole("textbox", { name: "Message" });
    await user.type(input, "first{Enter}second{Shift>}{Enter}{/Shift}third");
    expect(input).toHaveValue("first\nsecond\nthird");
    act(() => { input.closest("form")?.requestSubmit(); });
    expect(onAction).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();

    await user.tab();
    const launch = screen.getByRole("button", { name: "Launch" });
    expect(launch).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(launch).toBeEnabled());
    expect(onAction).toHaveBeenCalledTimes(1);
    await user.keyboard("[Space]");
    await waitFor(() => expect(launch).toBeEnabled());
    expect(onAction).toHaveBeenCalledTimes(2);
    expect(onSend).not.toHaveBeenCalled();
    expect(input).toHaveValue("first\nsecond\nthird");
  });

  it.each(["host", "pending", "sending", "thinking", "selecting", "read-only"] as const)("honors the %s lock for a host primary action", async (lock) => {
    const onAction = vi.fn();
    const execution = changingExecution();
    render(<ChatComposer onSend={vi.fn()} sending={lock === "sending"} submissionDisabled={lock === "thinking"} execution={{ ...execution, selecting: lock === "selecting", snapshot: { ...execution.snapshot!, readOnly: lock === "read-only" } }} primaryAction={{ label: "Launch", disabled: lock === "host", pending: lock === "pending", pendingLabel: "Launching…", onAction }} />);
    const launch = screen.getByRole("button", { name: lock === "pending" ? "Launching…" : "Launch" });
    expect(launch).toBeDisabled();
    await userEvent.click(launch);
    expect(onAction).not.toHaveBeenCalled();
    if (lock === "pending") expect(screen.getByRole("button", { name: /Run with/ })).toBeDisabled();
  });

  it("preserves the same textarea and draft when the host action becomes Send", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const buttonRef = createRef<HTMLButtonElement>();
    const { rerender } = render(<ChatComposer onSend={onSend} initialValue="Retained message" disabled primaryAction={{ label: "Launch", disabled: false, onAction: vi.fn(), buttonRef }} />);
    const input = screen.getByRole("textbox", { name: "Message" });
    const launch = screen.getByRole("button", { name: "Launch" });
    expect(buttonRef.current).toBe(launch);

    rerender(<ChatComposer onSend={onSend} />);
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(input);
    expect(input).toHaveValue("Retained message");
    expect(input).toBeEnabled();
    expect(launch).not.toBeInTheDocument();
    expect(buttonRef.current).toBeNull();
    expect(onSend).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(input).toHaveValue(""));
    expect(onSend).toHaveBeenCalledWith("Retained message");
  });

  it("merges the host button ref with keyboard help without changing activation", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    const buttonRef = createRef<HTMLButtonElement>();
    const { container } = render(<DashboardHelpProvider enabled><ChatComposer onSend={vi.fn()} disabled primaryAction={{ label: "Launch", disabled: false, onAction, buttonRef, help: { summary: "Launch this assistant.", details: "Share the disclosed form context, without sending your typed message." } }} /></DashboardHelpProvider>);
    const launch = screen.getByRole("button", { name: "Launch" });
    expect(buttonRef.current).toBe(launch);
    await user.tab();
    expect(launch).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Share the disclosed form context, without sending your typed message.");
    expect(launch).toHaveAccessibleDescription(/Share the disclosed form context/);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
    await user.keyboard("[Space]");
    await waitFor(() => expect(launch).toBeEnabled());
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(buttonRef.current).toBe(launch);
    await expectNoAccessibilityViolations(container);
  });

  it("does not reclaim focus from a host field after a delayed send acknowledgement", async () => {
    let finish: (() => void) | undefined;
    const pending = new Promise<void>((resolve) => { finish = resolve; });
    render(<><label>Host field<input /></label><ChatComposer onSend={() => pending} initialValue="One turn" /></>);
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    const field = screen.getByRole("textbox", { name: "Host field" });
    await userEvent.click(field);
    await userEvent.type(field, "human typing");
    await act(async () => { finish?.(); await pending; });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue(""));
    expect(field).toHaveFocus();
    expect(field).toHaveValue("human typing");
  });

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

  it("guards two submit events in the same render turn", async () => {
    let acknowledge: (() => void) | undefined;
    const pending = new Promise<void>((resolve) => {
      acknowledge = resolve;
    });
    const onSend = vi.fn(() => pending);
    render(<ChatComposer onSend={onSend} initialValue="one durable turn" />);
    const form = screen.getByRole("textbox").closest("form");
    expect(form).not.toBeNull();

    act(() => {
      form?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      form?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(onSend).toHaveBeenCalledTimes(1);
    await act(async () => acknowledge?.());
  });

  it("seeds from initialValue and reports the live draft, empty after send", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const onDraftChange = vi.fn();
    render(
      <ChatComposer
        onSend={onSend}
        initialValue="kept from before"
        onDraftChange={onDraftChange}
      />,
    );
    const input = screen.getByRole("textbox", { name: "Message" });
    expect(input).toHaveValue("kept from before");

    await userEvent.type(input, "!");
    expect(onDraftChange).toHaveBeenLastCalledWith("kept from before!");

    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(""));
  });

  it("grows a retained draft without showing a scrollbar before the cap", () => {
    const scrollHeight = vi
      .spyOn(HTMLTextAreaElement.prototype, "scrollHeight", "get")
      .mockReturnValue(72);
    mockTextareaGeometry();

    render(
      <ChatComposer
        onSend={vi.fn()}
        initialValue={"retained line one\nretained line two"}
      />,
    );

    const input = screen.getByRole("textbox");
    expect(input).toHaveStyle({ height: "74px", overflowY: "hidden" });
    scrollHeight.mockRestore();
  });

  it("scrolls internally only after reaching the CSS height cap", async () => {
    const scrollHeight = vi
      .spyOn(HTMLTextAreaElement.prototype, "scrollHeight", "get")
      .mockImplementation(function (this: HTMLTextAreaElement) {
        return this.value.includes("very long") ? 220 : 36;
      });
    mockTextareaGeometry();
    render(<ChatComposer onSend={vi.fn()} />);
    const input = screen.getByRole("textbox");

    await userEvent.type(input, "very long");
    expect(input).toHaveStyle({ height: "160px", overflowY: "auto" });

    await userEvent.clear(input);
    expect(input).toHaveStyle({ height: "38px", overflowY: "hidden" });
    scrollHeight.mockRestore();
  });

  it("disables input and Send when the composer is disabled", () => {
    render(<ChatComposer onSend={vi.fn()} disabled />);
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("can keep the shared model picker available before an explicit host Start", () => {
    const execution = { ...changingExecution(), selecting: false };
    const view = render(<ChatComposer onSend={vi.fn()} disabled execution={execution} />);
    expect(screen.getByRole("button", { name: /Run with/ })).toBeDisabled();
    view.rerender(<ChatComposer onSend={vi.fn()} disabled execution={execution} executionDisabled={false} />);
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Run with/ })).toBeEnabled();
    view.rerender(<ChatComposer onSend={vi.fn()} disabled execution={execution} executionDisabled={false} submissionDisabled />);
    expect(screen.getByRole("button", { name: /Run with/ })).toBeDisabled();
  });

  it("does not override server read-only or an in-flight selection with the picker lock", () => {
    const execution = changingExecution();
    const view = render(<ChatComposer onSend={vi.fn()} execution={execution} executionDisabled={false} />);
    expect(screen.getByRole("button", { name: /Run with/ })).toBeDisabled();
    view.rerender(<ChatComposer onSend={vi.fn()} execution={{ ...execution, selecting: false, snapshot: { ...execution.snapshot!, readOnly: true } }} executionDisabled={false} />);
    expect(screen.queryByRole("button", { name: /Run with/ })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
  });

  it("shows a pending state while a send is in flight", () => {
    render(<ChatComposer onSend={vi.fn()} sending />);
    expect(screen.getByText("Sending message")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sending message/ })).toBeDisabled();
  });

  it("retains a draft and blocks Enter while the execution pair is changing", async () => {
    const onSend = vi.fn();
    render(
      <ChatComposer
        onSend={onSend}
        execution={changingExecution()}
      />,
    );
    const input = screen.getByRole("textbox", { name: "Message" });

    await userEvent.type(input, "keep this draft{Enter}");

    expect(input).toHaveValue("keep this draft");
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("still delivers to a running conversation when catalog probing is unavailable", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatComposer
        onSend={onSend}
        execution={{
          ...changingExecution(),
          status: "error",
          selecting: false,
          currentAvailable: false,
        }}
      />,
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: "Message" }),
      "keep the durable turn{Enter}",
    );

    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("keep the durable turn"),
    );
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
