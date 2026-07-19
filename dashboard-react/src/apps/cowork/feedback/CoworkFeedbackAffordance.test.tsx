import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { resolveQuoteAnchor } from "../suggestions/anchor";
import { makeSuggestionEditor } from "../suggestions/__tests__/support";
import type { FeedbackCapture } from "../chat";
import { CoworkFeedbackAffordance } from "./CoworkFeedbackAffordance";
import {
  InMemoryCoworkFeedbackTransport,
  type CoworkFeedbackRequest,
  type CoworkFeedbackResponse,
  type CoworkFeedbackTransport,
} from "./feedbackClient";

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
  vi.restoreAllMocks();
});

const CONTENT = "<p>Make this sentence precise and clear enough.</p>";

const selectQuote = (ed: Editor, exact: string) => {
  const range = resolveQuoteAnchor(ed.state.doc, { exact, prefix: "", suffix: "" });
  if (range === null) throw new Error(`quote not found: ${exact}`);
  act(() => {
    ed.commands.setTextSelection(range);
  });
};

const renderAffordance = (
  options: {
    transport?: CoworkFeedbackTransport;
    storeId?: string;
    onCaptured?: (capture: FeedbackCapture) => void;
  } = {},
) => {
  editor = makeSuggestionEditor({ content: CONTENT });
  const onCaptured = options.onCaptured ?? vi.fn();
  const utils = render(
    <CoworkFeedbackAffordance
      editor={editor}
      documentId="doc-1"
      storeId={options.storeId ?? "store-1"}
      onCaptured={onCaptured}
      transport={options.transport}
    />,
  );
  return { ...utils, editor, onCaptured };
};

describe("CoworkFeedbackAffordance", () => {
  it("hides the trigger until the user selects a passage", () => {
    renderAffordance();
    expect(
      screen.queryByRole("button", { name: "Give feedback" }),
    ).not.toBeInTheDocument();
  });

  it("captures a selection, POSTs R9 verbatim, and reports the capture", async () => {
    const transport = new InMemoryCoworkFeedbackTransport();
    const onCaptured = vi.fn();
    const { editor: ed } = renderAffordance({ transport, onCaptured });

    selectQuote(ed, "precise");
    await userEvent.click(
      await screen.findByRole("button", { name: "Give feedback" }),
    );

    const input = screen.getByRole("textbox");
    await userEvent.type(input, "  make this measurable  ");
    await userEvent.click(screen.getByRole("button", { name: "Send feedback" }));

    await waitFor(() => expect(onCaptured).toHaveBeenCalledTimes(1));

    // The span the affordance anchored the feedback to (verified against api.py).
    const request = transport.lastRequest as CoworkFeedbackRequest;
    expect(request.documentId).toBe("doc-1");
    expect(request.storeId).toBe("store-1");
    expect(request.span.exact).toBe("precise");
    expect(request.span.prefix.endsWith("sentence ")).toBe(true);
    expect(request.span.node_id_hint).toBeNull();
    // The text is saved VERBATIM, whitespace and all (PRD section 5).
    expect(request.text).toBe("  make this measurable  ");

    // The capture handed up carries the R9 ids plus the anchor for the scroll-to seam.
    const capture = onCaptured.mock.calls[0][0] as FeedbackCapture;
    expect(capture.evidenceId).toBe("ev-doc-1");
    expect(capture.spanId).toBe("span-doc-1");
    expect(capture.conversationId).toBe("cowork-doc-doc-1");
    expect(capture.text).toBe("  make this measurable  ");
    expect(capture.anchor?.exact).toBe("precise");

    // The input closes on success.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("preserves the typed text and shows an inline error on a failed POST", async () => {
    const failing: CoworkFeedbackTransport = {
      submit: (): Promise<CoworkFeedbackResponse> =>
        Promise.reject(new Error("feedback capture failed with status 403")),
    };
    const onCaptured = vi.fn();
    const { editor: ed } = renderAffordance({ transport: failing, onCaptured });

    selectQuote(ed, "precise");
    await userEvent.click(
      await screen.findByRole("button", { name: "Give feedback" }),
    );
    await userEvent.type(screen.getByRole("textbox"), "do not lose this");
    await userEvent.click(screen.getByRole("button", { name: "Send feedback" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/403/);
    // The user's words survive the failure (never lose the feedback).
    expect(screen.getByRole("textbox")).toHaveValue("do not lose this");
    expect(onCaptured).not.toHaveBeenCalled();
  });

  it("disables the trigger and never POSTs without a live store id", async () => {
    const transport = new InMemoryCoworkFeedbackTransport();
    const { editor: ed } = renderAffordance({ transport, storeId: "" });

    selectQuote(ed, "precise");
    const trigger = await screen.findByRole("button", { name: "Give feedback" });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveAttribute("title", expect.stringMatching(/live scope/i));

    await userEvent.click(trigger);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(transport.lastRequest).toBeNull();
  });

  it("has no accessibility violations with the input open", async () => {
    const { editor: ed, container } = renderAffordance({
      transport: new InMemoryCoworkFeedbackTransport(),
    });
    selectQuote(ed, "precise");
    await userEvent.click(
      await screen.findByRole("button", { name: "Give feedback" }),
    );
    await screen.findByRole("textbox");
    await expectNoAccessibilityViolations(container);
  });
});
