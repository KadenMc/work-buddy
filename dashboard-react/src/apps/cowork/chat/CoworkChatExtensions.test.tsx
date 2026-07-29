import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CoworkActionSnapshotProvenance } from "./CoworkChatExtensions";

describe("CoworkActionSnapshotProvenance", () => {
  it("does not claim an unavailable frozen context was used", () => {
    render(
      <CoworkActionSnapshotProvenance
        author="assistant"
        context={{
          kind: "action_snapshot",
          actionSnapshotId: "action-unavailable",
          storeId: "store-1",
          documentId: "doc-1",
          targetKind: "text_quote",
          targetLabel: "Introduction",
          targetTextSha256: "a".repeat(64),
          projectionSha256: "b".repeat(64),
          capturedAt: "2026-07-28T00:00:00Z",
          consumption: {
            receiptId: "receipt-unavailable",
            userMessageId: "user-unavailable",
            fetchedAt: "2026-07-28T00:00:01Z",
            fetchOutcome: "unavailable",
            unavailableCode: "action_snapshot_unavailable",
          },
        }}
      />,
    );

    const provenance = screen.getByLabelText(
      "Frozen document context: Introduction",
    );
    expect(provenance).toHaveTextContent(
      "Couldn’t open Working on: Introduction",
    );
    expect(provenance).not.toHaveTextContent("Used Working on");
  });
});
