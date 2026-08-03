import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CoworkDocumentSummary } from "../contracts";
import { CoworkHttpClient } from "../providers/CoworkHttpClient";
import { CoworkRetirementDialog } from "./CoworkRetirementDialog";

const documentSummary: CoworkDocumentSummary = {
  documentId: "doc-1",
  path: "drafts/source.md",
  title: "Source",
  profile: "co_authored",
  sourceWriteback: "never",
  driftState: "clean",
  openProposalCount: 0,
  openFlagCount: 0,
};

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const preparedResponse = (intentId: string): Record<string, unknown> => ({
  intent_id: intentId,
  expires_at: "2026-07-31T20:00:00Z",
  document_id: documentSummary.documentId,
  consequence: "The source file and history are retained.",
  consequence_sha256: "a".repeat(64),
});

describe("CoworkRetirementDialog lifecycle settlement", () => {
  it("does not prepare until settlement succeeds and repeats settlement on Retry check", async () => {
    const user = userEvent.setup();
    const events: string[] = [];
    let settleAttempt = 0;
    const onSettleLifecycle = vi.fn(async () => {
      settleAttempt += 1;
      events.push(`settle:${String(settleAttempt)}`);
      if (settleAttempt === 1) throw new Error("Offline");
    });
    const fetchImpl = vi.fn(async () => {
      events.push("prepare");
      return json(preparedResponse("retire-1"));
    });

    render(
      <CoworkRetirementDialog
        storeId="store-1"
        document={documentSummary}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        onSettleLifecycle={onSettleLifecycle}
        onClose={vi.fn()}
        onRetired={vi.fn()}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Offline");
    expect(fetchImpl).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Retry check" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove from Co-work" }),
      ).toBeEnabled(),
    );

    expect(events).toEqual(["settle:1", "settle:2", "prepare"]);
    expect(onSettleLifecycle).toHaveBeenCalledTimes(2);
  });

  it("re-settles and prepares a fresh intent after commit requires compaction", async () => {
    const user = userEvent.setup();
    const events: string[] = [];
    const prepareKeys: string[] = [];
    let prepareCount = 0;
    let commitCount = 0;
    const onSettleLifecycle = vi.fn(async () => {
      events.push("settle");
    });
    const onRetired = vi.fn(async () => undefined);
    const fetchImpl = vi.fn(
      async (_input: RequestInfo | URL, init: RequestInit = {}) => {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        if (typeof body.idempotency_key === "string") {
          prepareCount += 1;
          prepareKeys.push(body.idempotency_key);
          events.push(`prepare:${String(prepareCount)}`);
          return json(preparedResponse(`retire-${String(prepareCount)}`));
        }
        commitCount += 1;
        events.push(`commit:${String(body.intent_id)}`);
        if (commitCount === 1) {
          return json(
            {
              error: {
                code: "retirement_compaction_required",
                message: "Settle the live document and check removal again.",
                retryable: true,
              },
            },
            409,
          );
        }
        return json({
          intent_id: body.intent_id,
          document_id: documentSummary.documentId,
          lifecycle: "retired",
          retired_at: "2026-07-31T19:00:00Z",
          doc_event_id: "event-retired",
          file_retained: true,
          history_retained: true,
        });
      },
    );

    render(
      <CoworkRetirementDialog
        storeId="store-1"
        document={documentSummary}
        client={new CoworkHttpClient(fetchImpl as typeof fetch)}
        onSettleLifecycle={onSettleLifecycle}
        onClose={vi.fn()}
        onRetired={onRetired}
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove from Co-work" }),
      ).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "Remove from Co-work" }));
    expect(await screen.findByRole("button", { name: "Retry check" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Retry check" }));
    await waitFor(() => expect(prepareKeys).toHaveLength(2));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove from Co-work" }),
      ).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "Remove from Co-work" }));
    await waitFor(() => expect(onRetired).toHaveBeenCalledTimes(1));

    expect(prepareKeys[1]).not.toBe(prepareKeys[0]);
    expect(events).toEqual([
      "settle",
      "prepare:1",
      "commit:retire-1",
      "settle",
      "prepare:2",
      "commit:retire-2",
    ]);
  });
});
