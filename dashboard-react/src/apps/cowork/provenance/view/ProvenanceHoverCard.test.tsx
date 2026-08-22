import { createRef } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ProvenanceHoverCard } from "./ProvenanceHoverCard";

describe("ProvenanceHoverCard", () => {
  it("explains a decorated passage passively and dismisses with Escape", () => {
    const root = document.createElement("div");
    const passage = document.createElement("span");
    passage.dataset.wbDecoration = "provenance-overlay";
    passage.dataset.wbAuthorship = "ai";
    passage.dataset.wbHumanReview = "not_reviewed";
    passage.dataset.wbSource = "paste";
    passage.dataset.wbSourceDetail =
      "plain text · source identity not verified";
    passage.dataset.wbContributors = "model:gpt-5 · asserted identity";
    passage.dataset.wbReviewers = "user-1 · enrolled local identity";
    passage.dataset.wbAttester = "user-1";
    passage.dataset.wbBasis = "user_attestation";
    passage.dataset.wbHistoryCount = "2";
    passage.dataset.wbProvenanceCurrentness = "multiple target states";
    root.append(passage);
    document.body.append(root);
    const ref = createRef<HTMLElement>();
    ref.current = root;
    render(<ProvenanceHoverCard rootRef={ref} active editorReady />);

    fireEvent.pointerOver(passage);
    expect(screen.getByRole("tooltip")).toHaveTextContent("ai authorship");
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "multiple target states",
    );
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "model:gpt-5 · asserted identity",
    );
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "user-1 · enrolled local identity",
    );
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "plain text · source identity not verified",
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).toBeNull();
    root.remove();
  });

  it("attaches after the active editor root mounts", () => {
    const ref = { current: null as HTMLElement | null };
    const { rerender } = render(
      <ProvenanceHoverCard rootRef={ref} active editorReady={false} />,
    );
    const root = document.createElement("div");
    const passage = document.createElement("span");
    passage.dataset.wbDecoration = "provenance-overlay";
    passage.dataset.wbAuthorship = "mixed";
    root.append(passage);
    document.body.append(root);
    ref.current = root;

    rerender(<ProvenanceHoverCard rootRef={ref} active editorReady />);
    fireEvent.pointerOver(passage);

    expect(screen.getByRole("tooltip")).toHaveTextContent("mixed authorship");
    root.remove();
  });

  it("refreshes an open card when an asynchronous projection replaces the hovered decoration", async () => {
    const root = document.createElement("div");
    const unrecorded = document.createElement("span");
    unrecorded.dataset.wbDecoration = "provenance-overlay";
    unrecorded.dataset.wbAuthorship = "unknown";
    unrecorded.dataset.wbSource = "unrecorded";
    unrecorded.dataset.wbProvenanceRecordState = "unrecorded";
    root.append(unrecorded);
    document.body.append(root);
    const ref = createRef<HTMLElement>();
    ref.current = root;
    render(<ProvenanceHoverCard rootRef={ref} active editorReady />);

    fireEvent.pointerOver(unrecorded, { clientX: 20, clientY: 20 });
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "No provenance recorded",
    );

    const recorded = document.createElement("span");
    recorded.dataset.wbDecoration = "provenance-overlay";
    recorded.dataset.wbAuthorship = "human";
    recorded.dataset.wbHumanReview = "not_applicable";
    recorded.dataset.wbSource = "direct_entry";
    recorded.dataset.wbSourceDetail = "plain text";
    recorded.dataset.wbProvenanceRecordState = "recorded";
    recorded.dataset.wbProvenanceCurrentness = "current";
    const originalElementFromPoint = document.elementFromPoint;
    Object.defineProperty(document, "elementFromPoint", {
      configurable: true,
      value: () => recorded,
    });
    unrecorded.replaceWith(recorded);

    await waitFor(() => {
      expect(screen.getByRole("tooltip")).toHaveTextContent("human authorship");
      expect(screen.getByRole("tooltip")).toHaveTextContent(
        "direct entry · plain text",
      );
    });
    Object.defineProperty(document, "elementFromPoint", {
      configurable: true,
      value: originalElementFromPoint,
    });
    root.remove();
  });

  it("describes pending delivery without asserting authorship, then settles in place", async () => {
    const root = document.createElement("div");
    const pending = document.createElement("span");
    pending.dataset.wbDecoration = "provenance-overlay";
    pending.dataset.wbAuthorship = "unknown";
    pending.dataset.wbHumanReview = "unknown";
    pending.dataset.wbSource = "direct_entry";
    pending.dataset.wbProvenanceRecordState = "pending";
    root.append(pending);
    document.body.append(root);
    const ref = createRef<HTMLElement>();
    ref.current = root;
    render(<ProvenanceHoverCard rootRef={ref} active editorReady />);

    fireEvent.pointerOver(pending, { clientX: 24, clientY: 24 });
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent("Recording provenance…");
    expect(tooltip).toHaveTextContent(/after the server confirms the record/u);
    expect(tooltip).not.toHaveTextContent("unknown authorship");
    expect(tooltip.querySelector("dl")).toBeNull();

    const recorded = document.createElement("span");
    recorded.dataset.wbDecoration = "provenance-overlay";
    recorded.dataset.wbAuthorship = "human";
    recorded.dataset.wbHumanReview = "not_applicable";
    recorded.dataset.wbSource = "direct_entry";
    recorded.dataset.wbSourceDetail = "Input: keyboard";
    recorded.dataset.wbProvenanceRecordState = "recorded";
    recorded.dataset.wbProvenanceCurrentness = "current";
    const originalElementFromPoint = document.elementFromPoint;
    Object.defineProperty(document, "elementFromPoint", {
      configurable: true,
      value: () => recorded,
    });
    pending.replaceWith(recorded);

    await waitFor(() => {
      expect(screen.getByRole("tooltip")).toHaveTextContent("human authorship");
      expect(screen.getByRole("tooltip")).not.toHaveTextContent(
        "Recording provenance…",
      );
    });
    Object.defineProperty(document, "elementFromPoint", {
      configurable: true,
      value: originalElementFromPoint,
    });
    root.remove();
  });

  it("yields to an expanded editor selection and cannot reopen over its action", () => {
    const root = document.createElement("div");
    root.tabIndex = 0;
    const passage = document.createElement("span");
    passage.dataset.wbDecoration = "provenance-overlay";
    passage.dataset.wbAuthorship = "human";
    passage.textContent = "Selected passage";
    root.append(passage);
    document.body.append(root);
    const ref = createRef<HTMLElement>();
    ref.current = root;
    render(<ProvenanceHoverCard rootRef={ref} active editorReady />);

    fireEvent.pointerOver(passage);
    expect(screen.getByRole("tooltip")).toHaveTextContent("human authorship");

    const range = document.createRange();
    range.selectNodeContents(passage);
    const selection = document.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    fireEvent(document, new Event("selectionchange"));

    expect(screen.queryByRole("tooltip")).toBeNull();
    fireEvent.pointerOver(passage);
    expect(screen.queryByRole("tooltip")).toBeNull();

    selection?.removeAllRanges();
    fireEvent.pointerOver(passage);
    expect(screen.getByRole("tooltip")).toHaveTextContent("human authorship");
    root.remove();
  });
});
