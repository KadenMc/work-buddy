import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TruthAnalysisRun } from "./contracts";
import { TruthAnalysisReview } from "./TruthAnalysisReview";
import truthStyles from "./styles.css?raw";

const run: TruthAnalysisRun = {
  schema: "wb.cowork.truth-analysis-run/v1",
  analysisRunId: "run-1",
  storeId: "store-1",
  documentId: "doc-1",
  status: "completed",
  targetChoice: "current_selection",
  targetLabel: "Selected passage",
  capturedAt: "2026-08-09T12:00:00Z",
  structuredHeadSha256: "a".repeat(64),
  projectionSha256: "b".repeat(64),
  execution: {
    providerId: "claude-code",
    modelId: "sonnet",
    providerLabel: "Claude Code",
    modelLabel: "Sonnet",
  },
  candidates: [
    {
      candidateId: "candidate-1",
      canonicalSha256: "c".repeat(64),
      status: "pending",
      decision: null,
      proposition: "A bounded proposition.",
      claimKind: "factual",
      confidenceExtraction: 0.84,
      expression: {
        role: "paraphrase",
        quote: "A selected factual passage.",
        selector: {
          kind: "text_quote",
          exact: "A selected factual passage.",
          prefix: "",
          suffix: "",
          start: 0,
          end: 27,
        },
      },
      existingClaimMatch: {
        claimId: "claim-1",
        proposition: "The existing canonical proposition.",
        relationship: "equivalent",
        confidence: 0.91,
        rationale: "The propositions have the same meaning.",
      },
      evidence: [
        {
          evidenceCandidateId: "evidence-1",
          sourceKind: "truth_span",
          attachable: true,
          relationship: "supports",
          quote: "Recorded support.",
          sourceLocator: "truth://evidence/1",
          sourceTitle: "Recorded evidence",
          trustClass: "human_authored",
          integrityState: "recorded",
          capture: null,
          rationale: "Directly supports the claim.",
        },
        {
          evidenceCandidateId: "evidence-2",
          sourceKind: "truth_span",
          attachable: false,
          relationship: "contradicts",
          quote: "Recorded contradiction.",
          sourceLocator: "truth://evidence/2",
          sourceTitle: "Contradicting evidence",
          trustClass: "human_authored",
          integrityState: "recorded",
          capture: null,
          rationale: "Directly contradicts the claim.",
        },
        {
          evidenceCandidateId: "evidence-3",
          sourceKind: "passage_citation",
          attachable: false,
          relationship: "supports",
          quote: "An unresolved citation cue.",
          sourceLocator: "https://example.test/source",
          sourceTitle: "Passage citation",
          trustClass: "unattested",
          integrityState: "unresolved",
          capture: null,
          rationale: "The passage appears to cite this source.",
        },
        {
          evidenceCandidateId: "evidence-4",
          sourceKind: "web_fetch",
          attachable: true,
          relationship: "supports",
          quote: "Captured external support.",
          sourceLocator: "https://research.example.test/article",
          sourceTitle: "Research source",
          trustClass: "external_quarantined",
          integrityState: "captured_runtime",
          capture: {
            textTruncated: true,
            capturedTextBytes: 65_536,
            extractedTextBytes: 90_000,
            capturedTextSha256: "2".repeat(64),
            fullExtractedTextSha256: "3".repeat(64),
            maximumCapturedTextBytes: 65_536,
          },
          rationale: "The fetched source directly supports the claim.",
        },
      ],
      sourceCoverage: [],
      limitations: ["The web was not searched."],
    },
  ],
  sourceCoverage: [
    {
      source: "existing_truth",
      status: "searched",
      detail: "Recorded Truth was checked.",
      externalEgress: false,
    },
    {
      source: "web",
      status: "not_searched",
      detail: null,
      externalEgress: false,
    },
  ],
  limitations: ["No external source search was performed."],
  error: null,
  createdAt: "2026-08-09T12:00:00Z",
  finishedAt: "2026-08-09T12:00:05Z",
};

const expandCandidate = async (): Promise<void> => {
  await userEvent.click(screen.getByRole("button", { name: "Review" }));
  await userEvent.click(
    screen.getByRole("button", { name: /A bounded proposition/u }),
  );
};

describe("TruthAnalysisReview", () => {
  it("connects an equivalent existing claim without silently creating a duplicate", async () => {
    const onDecide = vi.fn(async () => undefined);
    render(<TruthAnalysisReview run={run} onDecide={onDecide} />);
    await expandCandidate();

    expect(
      screen.getByRole("checkbox", { name: "Attach as support: Recorded evidence" }),
    ).not.toBeChecked();
    expect(
      screen.queryByRole("checkbox", { name: "Attach as support: Contradicting evidence" }),
    ).toBeNull();
    expect(
      screen.queryByRole("checkbox", { name: "Attach as support: Passage citation" }),
    ).toBeNull();
    expect(
      screen.getByRole("checkbox", { name: "Attach as support: Research source" }),
    ).not.toBeChecked();
    expect(screen.getByText(/Citation cue · Unattested · Unresolved/u)).toBeVisible();
    const connect = screen.getByRole("button", { name: "Connect existing claim" });
    await userEvent.click(connect);

    expect(onDecide).toHaveBeenCalledWith({
      analysisRunId: "run-1",
      candidateId: "candidate-1",
      decision: "connect_existing",
      expectedCanonicalSha256: "c".repeat(64),
      existingClaimId: "claim-1",
      edits: {
        proposition: "A bounded proposition.",
        claimKind: "factual",
        expressionRole: "paraphrase",
        evidenceCandidateIds: [],
      },
    });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Hide" }),
      ).toHaveFocus(),
    );
  });

  it("keeps an equivalent match when the inferred expression role is corrected", async () => {
    const onDecide = vi.fn(async () => undefined);
    render(<TruthAnalysisReview run={run} onDecide={onDecide} />);
    await expandCandidate();
    await userEvent.click(screen.getByRole("button", { name: "Edit details" }));
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "How the passage expresses it" }),
      "quote",
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Connect existing claim" }),
    );
    expect(onDecide).toHaveBeenCalledWith(
      expect.objectContaining({
        decision: "connect_existing",
        existingClaimId: "claim-1",
        edits: expect.objectContaining({ expressionRole: "quote" }),
      }),
    );
  });

  it("passively focuses an expanded candidate and reveals it only on request", async () => {
    const onFocusCandidate = vi.fn();
    const onRevealCandidate = vi.fn();
    render(
      <TruthAnalysisReview
        run={run}
        onDecide={vi.fn(async () => undefined)}
        onFocusCandidate={onFocusCandidate}
        onRevealCandidate={onRevealCandidate}
      />,
    );
    await expandCandidate();
    await waitFor(() =>
      expect(onFocusCandidate).toHaveBeenLastCalledWith(run.candidates[0]),
    );
    expect(onRevealCandidate).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Show in document" }));
    expect(onRevealCandidate).toHaveBeenCalledWith(run.candidates[0]);
    const source = screen.getByRole("link", { name: "Passage citation" });
    expect(source).toHaveAttribute("href", "https://example.test/source");
    expect(source).toHaveAttribute("target", "_blank");
    expect(source).toHaveAttribute("rel", "noopener noreferrer");

    await userEvent.click(
      screen.getByRole("button", { name: /A bounded proposition/u }),
    );
    await waitFor(() => expect(onFocusCandidate).toHaveBeenLastCalledWith(null));
  });

  it("makes evidence attachment explicit and saves edited text as a new proposal", async () => {
    const onDecide = vi.fn(async () => undefined);
    render(<TruthAnalysisReview run={run} onDecide={onDecide} />);
    await expandCandidate();
    await userEvent.click(screen.getByRole("button", { name: "Edit details" }));
    await userEvent.clear(screen.getByRole("textbox", { name: "Claim" }));
    await userEvent.type(
      screen.getByRole("textbox", { name: "Claim" }),
      "A distinct proposition.",
    );
    const recordedEvidence = screen.getByText("Recorded evidence").closest("li");
    expect(recordedEvidence).not.toBeNull();
    await userEvent.click(within(recordedEvidence!).getByText("Attach as support"));
    expect(
      within(recordedEvidence!).getByRole("checkbox", {
        name: "Attach as support: Recorded evidence",
      }),
    ).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Add as proposed" }));

    expect(onDecide).toHaveBeenCalledWith({
      analysisRunId: "run-1",
      candidateId: "candidate-1",
      decision: "save_as_proposed",
      expectedCanonicalSha256: "c".repeat(64),
      edits: {
        proposition: "A distinct proposition.",
        claimKind: "factual",
        expressionRole: "paraphrase",
        evidenceCandidateIds: ["evidence-1"],
      },
    });
  });

  it("warns about a partially captured web source before attachment", async () => {
    render(<TruthAnalysisReview run={run} onDecide={vi.fn(async () => undefined)} />);
    await expandCandidate();
    const source = screen.getByText("Research source").closest("li");
    expect(source).not.toBeNull();
    expect(within(source!).getByText("Partially captured.")).toBeVisible();
    expect(source!.textContent).toContain(
      "65,536 of 90,000 extracted bytes were available for analysis.",
    );
    expect(
      source!.textContent!.indexOf("Partially captured."),
    ).toBeLessThan(source!.textContent!.indexOf("Attach as support"));
    expect(
      within(source!).getByRole("checkbox", {
        name: "Attach as support: Research source",
      }),
    ).not.toBeChecked();
  });

  it("stays compact until review while retaining reported coverage and limitations", async () => {
    render(<TruthAnalysisReview run={run} onDecide={vi.fn(async () => undefined)} />);
    expect(screen.getByText("Analysis coverage")).not.toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(screen.getByText("Analysis coverage")).toBeVisible();
    expect(screen.getByText("Web")).toBeVisible();
    expect(screen.getByText("Not checked")).toBeVisible();
    expect(screen.getByText("No external source search was performed.")).toBeVisible();
  });

  it("distinguishes supplied context from work completed on a failed run", async () => {
    render(
      <TruthAnalysisReview
        run={{
          ...run,
          status: "failed",
          candidates: [],
          error: "The account-model session timed out. You can analyze again.",
          sourceCoverage: [{
            source: "selected_passage",
            status: "supplied",
            detail: "The exact passage capture was supplied to the run.",
            externalEgress: false,
          }],
        }}
        onDecide={vi.fn(async () => undefined)}
      />,
    );

    expect(
      screen.getByText("The account-model session timed out. You can analyze again."),
    ).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(screen.getByText("Analysis coverage")).toBeVisible();
    expect(screen.getByText("Provided as context")).toBeVisible();
    expect(screen.queryByText("Checked")).not.toBeInTheDocument();
  });

  it("keeps the outer Truth body as the only scroll owner", () => {
    const candidateRule = truthStyles.match(
      /\.wb-cowork-truth-analysis__candidates\s*\{([^}]*)\}/u,
    )?.[1] ?? "";
    expect(candidateRule).not.toMatch(/overflow|max-block-size|overscroll/u);
  });

  it("pads and separates run-level coverage inside the collapsible content", () => {
    const coverageRule = truthStyles.match(
      /\.wb-cowork-truth-analysis__content\s*>\s*\.wb-cowork-truth-analysis__coverage,\s*\.wb-cowork-truth-analysis__run-limitations\s*\{([^}]*)\}/u,
    )?.[1] ?? "";
    expect(coverageRule).toMatch(/padding:/u);
    expect(coverageRule).toMatch(/border-block-start:/u);
  });
});
