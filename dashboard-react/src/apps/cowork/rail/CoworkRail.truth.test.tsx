import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DashboardHelpProvider } from "../../../dashboard/help";
import { createDemoChatProvider } from "./chatFixture";
import { CoworkRail } from "./CoworkRail";
import { InMemoryReviewProvider } from "./InMemoryReviewProvider";
import type {
  TruthClaimDetail,
  TruthClaimSummary,
  TruthClaimsSnapshot,
  TruthRailProvider,
} from "../truth";
import { TruthStore } from "../truth";

class MemoryStorage implements Storage {
  readonly #values = new Map<string, string>();

  get length(): number {
    return this.#values.size;
  }

  clear(): void {
    this.#values.clear();
  }

  getItem(key: string): string | null {
    return this.#values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.#values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.#values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.#values.set(key, value);
  }
}

const claim: TruthClaimSummary = {
  claimId: "claim-needing-review",
  proposition: "The document has a review rail.",
  claimKind: "fact",
  canonicalSha256: "canonical-claim",
  scope: "store",
  baseStatus: "proposed",
  needsReview: false,
  health: "clean",
  healthReason: null,
  voided: false,
  redacted: false,
  validFrom: null,
  validTo: null,
  effectiveValidFrom: null,
  effectiveValidTo: null,
  evidenceCount: 0,
  connectionCount: 0,
  connections: [],
  createdAt: "2026-08-04T12:00:00Z",
  createdBy: { kind: "agent", ref: "run-1" },
  isFact: false,
  availableActions: ["confirm", "reject", "redact"],
};

const claimDetail: TruthClaimDetail = {
  ...claim,
  structured: {},
  receipts: [],
  lifecycle: [
    {
      eventId: "event-1",
      status: "proposed",
      at: "2026-08-04T12:00:00Z",
      actorKind: "agent",
      actorRef: "run-1",
      note: null,
    },
  ],
  conflicts: [],
  derivations: [],
  support: {
    supportSpanIds: [],
    usableSpanIds: [],
    quarantinedOnly: false,
    agentAuthoredOnly: false,
    storeDerivedOnly: false,
  },
  premises: {
    localUnconfirmed: [],
    unresolvedUris: [],
    confirmed: true,
  },
  decisionBinding: {
    payloadSha256: "payload-hash",
    contextSha256: "context-hash",
    agentAuthoredOnly: false,
  },
};

const snapshot: TruthClaimsSnapshot = {
  schema: "cowork-truth/v1",
  storeId: "demo-store",
  documentId: "demo-doc",
  scope: "document",
  filter: "all",
  claims: [claim],
  counts: {
    all: 1,
    facts: 0,
    proposed: 1,
    needsReview: 0,
    challenged: 0,
    unconnected: 1,
  },
  capabilities: {
    canObserve: true,
    canModify: true,
    canDecide: true,
    allowedClaimKinds: ["fact"],
    mutationUnavailableReason: null,
  },
  readOnly: false,
  nextOffset: null,
};

const setupTruth = () => {
  const provider: TruthRailProvider = {
    load: vi.fn(async (query) => ({
      ...snapshot,
      scope: query.scope,
      filter: query.filter,
    })),
    loadClaim: vi.fn(async () => claimDetail),
    subscribe: vi.fn(() => () => undefined),
    proposeClaim: vi.fn(async () => ({
      ok: true,
      claimId: claim.claimId,
      claimCreated: true,
      expressionId: null,
      expressionCreated: false,
      status: "proposed",
    })),
    connectClaim: vi.fn(async () => ({
      ok: true,
      claimId: claim.claimId,
      claimCreated: false,
      expressionId: null,
      expressionCreated: false,
      status: "proposed",
    })),
    decideClaim: vi.fn(async () => ({
      ok: true,
      claimId: claim.claimId,
      claimCreated: false,
      expressionId: null,
      expressionCreated: false,
      status: "confirmed",
    })),
  };
  return { provider, store: new TruthStore() };
};

const renderTruthRail = ({ help = false }: { readonly help?: boolean } = {}) => {
  const truth = setupTruth();
  const rail = (
    <CoworkRail
      documentId="demo-doc"
      storeId="demo-store"
      reviewProvider={new InMemoryReviewProvider()}
      chat={{
        kind: "ready",
        provider: createDemoChatProvider("conv-1"),
        conversationId: "conv-1",
        draftStorageId: "conv-1",
        agent: {
          status: "running",
          alive: true,
          started: true,
          error: null,
        },
      }}
      truth={truth}
      storage={new MemoryStorage()}
    />
  );
  return {
    ...render(
      help ? (
        <DashboardHelpProvider enabled>{rail}</DashboardHelpProvider>
      ) : (
        rail
      ),
    ),
    ...truth,
  };
};

describe("CoworkRail Truth integration", () => {
  it("presents Review, Truth, and Chat as one roving desktop tablist", async () => {
    const user = userEvent.setup();
    renderTruthRail();

    const review = screen.getByRole("tab", { name: "Review" });
    const truth = screen.getByRole("tab", { name: "Truth" });
    const chat = screen.getByRole("tab", { name: /Chat/ });
    expect(review).toHaveAttribute("aria-selected", "true");
    expect(review).toHaveAttribute("tabindex", "0");
    expect(truth).toHaveAttribute("tabindex", "-1");
    expect(chat).toHaveAttribute("tabindex", "-1");

    review.focus();
    await user.keyboard("{ArrowRight}");
    expect(truth).toHaveFocus();
    expect(truth).toHaveAttribute("aria-selected", "true");
    expect(truth).toHaveAttribute("tabindex", "0");
    expect(review).toHaveAttribute("tabindex", "-1");

    await user.keyboard("{End}");
    expect(chat).toHaveFocus();
    expect(chat).toHaveAttribute("aria-selected", "true");
  });

  it("uses the existing Review, Truth, and Chat tabs as hover-help targets", () => {
    renderTruthRail({ help: true });

    for (const name of ["Review", "Truth", /Chat/]) {
      expect(screen.getByRole("tab", { name })).toHaveAttribute(
        "data-help-target",
        "true",
      );
    }
  });

  it("routes a Review attention item to the exact Truth claim detail", async () => {
    const user = userEvent.setup();
    const { provider, store } = renderTruthRail();

    const attentionItem = await screen.findByRole(
      "button",
      { name: /The document has a review rail.*Proposed/ },
      { timeout: 5_000 },
    );
    await user.click(attentionItem);

    expect(screen.getByRole("tab", { name: "Truth" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(store.getState().selectedClaimId).toBe(claim.claimId);
    await waitFor(() =>
      expect(provider.loadClaim).toHaveBeenCalledWith(claim.claimId),
    );
    expect(
      await screen.findByRole("heading", { name: claim.proposition }),
    ).toBeVisible();
  });
});
