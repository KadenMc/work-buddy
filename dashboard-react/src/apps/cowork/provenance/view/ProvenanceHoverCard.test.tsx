import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
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
    passage.dataset.wbSourceDetail = "plain text · source identity not verified";
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
    expect(screen.getByRole("tooltip")).toHaveTextContent("multiple target states");
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
});
