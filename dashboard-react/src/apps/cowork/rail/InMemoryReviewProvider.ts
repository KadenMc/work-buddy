/**
 * A deterministic in-memory ReviewRailProvider. It reproduces the SP-6 review
 * scene (two insertions, one deletion, one flag, one claim) so the rail, its
 * tests, and a development harness all render the same content the mockups
 * showed. It performs no input and is not a live transport, a live provider
 * supplies that behind the same seam.
 */

import type {
  ProposalVerbKind,
  ReviewClaim,
  ReviewExpression,
  ReviewProposal,
  ReviewRailData,
  SittingItemResult,
  SittingResult,
} from "./contracts";
import type {
  ReviewInvalidationListener,
  ReviewRailProvider,
  ReviewUnsubscribe,
  SittingSubmission,
} from "./provider";

/** A short deterministic 64-hex stand-in for a canonical hash. */
function fakeSha(seed: string): string {
  let hash = 0;
  for (let index = 0; index < seed.length; index += 1) {
    hash = (hash * 31 + seed.charCodeAt(index)) >>> 0;
  }
  const base = hash.toString(16).padStart(8, "0");
  return base.repeat(8).slice(0, 64);
}

const RESEARCH_AGENT = {
  model: "research-agent",
  modelSource: "session-manifest",
  sessionId: "sess-demo",
  surface: "mcp",
} as const;

function demoProposals(): ReviewProposal[] {
  return [
    {
      proposalId: "s1",
      kind: "edit",
      changeType: "insertion",
      quoteAnchor: {
        exact: "the active collector set",
        prefix: "The cache keys on ",
        suffix: ", so a bundle is reused",
      },
      replacement: "the active collector set and the vault content hash",
      rationale:
        "Keys must include vault state, or edits made outside the collector set go unnoticed by eviction.",
      tldr: "Add the vault content hash to the cache key.",
      producer: RESEARCH_AGENT,
      epistemicState: "ai_proposed",
      baseDocSha256: fakeSha("base"),
      canonicalSha256: fakeSha("s1"),
      baseOk: true,
      status: "open",
      fixesRef: null,
      claimRefs: [],
      createdAt: "2026-07-17T12:00:00Z",
      anchorLabel: "paragraph 2",
      documentOrder: 20,
    },
    {
      proposalId: "s2",
      kind: "edit",
      changeType: "insertion",
      quoteAnchor: {
        exact: "every collector output",
        prefix: "Keys on a digest of ",
        suffix: ".",
      },
      replacement:
        "every collector output, which makes eviction exact at the cost of a hashing pass",
      rationale:
        "Names the tradeoff the reader will ask about, exactness versus the per-invalidation hash.",
      tldr: "Name the exactness versus hashing-cost tradeoff.",
      producer: RESEARCH_AGENT,
      epistemicState: "ai_proposed",
      baseDocSha256: fakeSha("base"),
      canonicalSha256: fakeSha("s2"),
      baseOk: true,
      status: "open",
      fixesRef: null,
      claimRefs: [],
      createdAt: "2026-07-17T12:00:00Z",
      anchorLabel: "list, item 2",
      documentOrder: 40,
    },
    {
      proposalId: "s3",
      kind: "edit",
      changeType: "deletion",
      quoteAnchor: {
        exact: "always ",
        prefix: "We ",
        suffix: "rebuild the bundle",
      },
      replacement: "",
      rationale:
        "Always overstates it. The next clause already scopes the rebuild to reported changes.",
      tldr: "Drop the overstated always.",
      producer: RESEARCH_AGENT,
      epistemicState: "ai_proposed",
      baseDocSha256: fakeSha("base"),
      canonicalSha256: fakeSha("s3"),
      baseOk: true,
      status: "open",
      fixesRef: null,
      claimRefs: [],
      createdAt: "2026-07-17T12:00:00Z",
      anchorLabel: "paragraph 4",
      documentOrder: 60,
    },
    {
      proposalId: "f1",
      kind: "flag",
      quoteAnchor: {
        exact: "cold-start latency dropped from 1.8 s to 1.1 s after prewarming",
        prefix: "Benchmarks on the reference machine show ",
        suffix: ".",
      },
      replacement: null,
      rationale:
        "The figure needs a citation to the benchmark file, or it reads as an unsourced assertion.",
      tldr: "Cite the benchmark file for this figure.",
      producer: RESEARCH_AGENT,
      epistemicState: "ai_proposed",
      baseDocSha256: fakeSha("base"),
      canonicalSha256: fakeSha("f1"),
      baseOk: true,
      status: "open",
      fixesRef: null,
      claimRefs: [],
      createdAt: "2026-07-17T12:00:00Z",
      anchorLabel: "paragraph 6",
      documentOrder: 80,
    },
  ];
}

function demoExpressions(): ReviewExpression[] {
  return [
    {
      expressionId: "x1",
      spanId: "sp-cl1",
      nodeIdHint: null,
      quote: "cold-start latency dropped from 1.8 s to 1.1 s after prewarming",
      claimRef: "wb-truth://demo/claim/cl1",
      claimStatus: "confirmed",
      claimKind: "measurement",
    },
  ];
}

function demoClaims(): ReviewClaim[] {
  return [
    {
      claimId: "cl1",
      proposition:
        "Cold-start latency dropped from 1.8 s to 1.1 s after prewarming.",
      status: "confirmed",
      claimKind: "measurement",
      canonicalSha256: fakeSha("cl1"),
      rationale:
        "This sentence expresses a measured claim. Its evidence is two benchmark runs on the reference machine.",
      receipts: [
        {
          evidenceId: "ev-1",
          quote: "prewarm run A: cold-start 1.12 s",
          sourceLocator: "benchmarks/prewarm-a.json",
          trustClass: "measurement",
        },
        {
          evidenceId: "ev-2",
          quote: "prewarm run B: cold-start 1.08 s",
          sourceLocator: "benchmarks/prewarm-b.json",
          trustClass: "measurement",
        },
      ],
      anchorLabel: "paragraph 6",
      documentOrder: 80,
    },
  ];
}

/** The default fixture scene shared by the rail tests and any dev harness. */
export function demoReviewData(): ReviewRailData {
  const proposals = demoProposals();
  return {
    documentId: "demo-doc",
    title: "context-bundle-cache.md",
    drift: {
      state: "clean",
      openProposalCount: proposals.length,
      openFlagCount: proposals.filter((item) => item.kind === "flag").length,
      lastMaterializedSha256: fakeSha("materialized"),
      currentFileSha256: fakeSha("materialized"),
    },
    proposals,
    expressions: demoExpressions(),
    provenanceSpans: [
      {
        spanId: "sp-c1",
        quote:
          "Prewarming the cache on sidecar start removes the cold-start penalty",
        trustState: "ai_confirmed",
        producer: RESEARCH_AGENT,
        approvalGestureId: "g-approve-c1",
      },
    ],
    claims: demoClaims(),
  };
}

/** Map a proposal verb to its S4 result kind for the fixture sitting. */
function resultForVerb(verb: ProposalVerbKind): SittingItemResult["result"] {
  switch (verb) {
    case "confirm":
    case "edit_confirm":
      return "applied";
    case "reject_plain":
    case "reject_as_false":
    case "reject_as_preference":
    case "dismiss":
      return "closed";
    case "redirect":
      return "kept_open_redirected";
    case "defer":
      return "kept_open_deferred";
    case "endorse":
      return "kept_open_endorsed";
  }
}

export interface InMemoryReviewSeed {
  readonly data?: ReviewRailData;
}

export class InMemoryReviewProvider implements ReviewRailProvider {
  private data: ReviewRailData;
  private readonly listeners = new Set<ReviewInvalidationListener>();

  constructor(seed: InMemoryReviewSeed = {}) {
    this.data = seed.data ?? demoReviewData();
  }

  async load(): Promise<ReviewRailData> {
    return this.data;
  }

  subscribe(onInvalidate: ReviewInvalidationListener): ReviewUnsubscribe {
    this.listeners.add(onInvalidate);
    return () => {
      this.listeners.delete(onInvalidate);
    };
  }

  private notify(): void {
    for (const listener of this.listeners) listener();
  }

  async submitSitting(submission: SittingSubmission): Promise<SittingResult> {
    const decided = new Map(
      submission.proposalDecisions.map((item) => [item.proposalId, item.verb]),
    );
    const results: SittingItemResult[] = submission.proposalDecisions.map(
      (item) => {
        const proposal = this.data.proposals.find(
          (candidate) => candidate.proposalId === item.proposalId,
        );
        const baseOk = proposal?.baseOk ?? true;
        return {
          proposalId: item.proposalId,
          verb: item.verb,
          result: resultForVerb(item.verb),
          baseOk,
          gestureId: `g-${item.proposalId}`,
          error: null,
        };
      },
    );

    // Reflect the sitting: applied and closed items leave the open set, routed
    // items stay open. A live route re-derives from the ledger, the fixture
    // just mutates its own snapshot so the loop is demonstrable.
    const remaining = this.data.proposals.filter((proposal) => {
      const verb = decided.get(proposal.proposalId);
      if (verb === undefined) return true;
      const result = resultForVerb(verb);
      return result === "kept_open_redirected" || result === "kept_open_deferred";
    });
    this.data = {
      ...this.data,
      proposals: remaining,
      drift: {
        ...this.data.drift,
        openProposalCount: remaining.length,
        openFlagCount: remaining.filter((item) => item.kind === "flag").length,
      },
    };
    this.notify();

    return {
      ok: true,
      partial: results.some((result) => result.result === "error"),
      results,
    };
  }
}
