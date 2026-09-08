import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import { CoworkPassageRequest, type CoworkPassageRequestProps } from "./CoworkPassageRequest";
import { InMemoryCoworkFeedbackTransport, type CoworkFeedbackRequest } from "./feedbackClient";

const anchor = { exact: "A frozen passage.", prefix: "Before ", suffix: " After" };
const defaults = (): CoworkPassageRequestProps => ({
  anchor, documentId: "doc", storeId: "store", transport: new InMemoryCoworkFeedbackTransport(), onCaptured: vi.fn(), onClose: vi.fn(),
});
const renderRequest = (overrides: Partial<CoworkPassageRequestProps> = {}) => {
  const props = { ...defaults(), ...overrides };
  return { ...render(<CoworkPassageRequest {...props} />), props };
};

describe("CoworkPassageRequest", () => {
  it("focuses the note, traps tab navigation, and restores focus on Escape", async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return <><button onClick={() => setOpen(true)}>Open request</button>
        {open ? <CoworkPassageRequest {...defaults()} onClose={() => setOpen(false)} /> : null}</>;
    }
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Open request" });
    await userEvent.click(trigger);
    const input = screen.getByRole("textbox", { name: "Change request" });
    await waitFor(() => expect(input).toHaveFocus());
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Send change request" })).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await userEvent.tab();
    expect(input).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("preserves verbatim text and its frozen anchor after a failed send", async () => {
    const memory = new InMemoryCoworkFeedbackTransport();
    const submit = vi.fn().mockRejectedValueOnce(new Error("Connection unavailable."))
      .mockImplementation((request: CoworkFeedbackRequest) => memory.submit(request));
    const { props } = renderRequest({ transport: { submit } });
    const input = screen.getByRole("textbox", { name: "Change request" });
    await userEvent.type(input, "  Preserve these words.  ");
    await userEvent.click(screen.getByRole("button", { name: "Send change request" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection unavailable.");
    expect(input).toHaveValue("  Preserve these words.  ");
    expect(props.onCaptured).not.toHaveBeenCalled();
    expect(props.onClose).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Send change request" }));
    await waitFor(() => expect(props.onCaptured).toHaveBeenCalledOnce());
    expect(submit).toHaveBeenCalledTimes(2);
    expect(memory.lastRequest).toEqual({ documentId: "doc", storeId: "store", text: "  Preserve these words.  ", span: { ...anchor, node_id_hint: null } });
  });

  it("does not resend a saved request when the Chat handoff fails", async () => {
    const memory = new InMemoryCoworkFeedbackTransport();
    const submit = vi.fn((request: CoworkFeedbackRequest) => memory.submit(request));
    const onCaptured = vi.fn().mockImplementationOnce(() => { throw new Error("Chat unavailable"); });
    renderRequest({ transport: { submit }, onCaptured });
    await userEvent.type(screen.getByRole("textbox"), "Please update this.");
    await userEvent.click(screen.getByRole("button", { name: "Send change request" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Your request was sent, but Chat could not open.");
    expect(screen.getByRole("textbox")).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Open in Chat" }));
    await waitFor(() => expect(onCaptured).toHaveBeenCalledTimes(2));
    expect(submit).toHaveBeenCalledOnce();
  });

  it("keeps the request open and prevents duplicate submission while sending", async () => {
    let finish!: () => void;
    const gate = new Promise<void>((resolve) => { finish = resolve; });
    const memory = new InMemoryCoworkFeedbackTransport();
    const submit = vi.fn(async (request: CoworkFeedbackRequest) => { await gate; return memory.submit(request); });
    const { props } = renderRequest({ transport: { submit } });
    await userEvent.type(screen.getByRole("textbox"), "Please update this.");
    await userEvent.click(screen.getByRole("button", { name: "Send change request" }));
    expect(screen.getByRole("button", { name: "Sending…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await userEvent.keyboard("{Escape}");
    expect(props.onClose).not.toHaveBeenCalled();
    await act(async () => { finish(); });
    await waitFor(() => expect(props.onClose).toHaveBeenCalledOnce());
    expect(submit).toHaveBeenCalledOnce();
  });

  it("rejects an empty note and supports cancellation without posting", async () => {
    const submit = vi.fn();
    const { props } = renderRequest({ transport: { submit } });
    await userEvent.click(screen.getByRole("button", { name: "Send change request" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Write a note before sending.");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(props.onClose).toHaveBeenCalledOnce();
    expect(submit).not.toHaveBeenCalled();
  });

  it("clears automated accessibility checks with a labeled passage and note", async () => {
    renderRequest();
    await expectNoAccessibilityViolations(screen.getByRole("dialog"));
  });
});
