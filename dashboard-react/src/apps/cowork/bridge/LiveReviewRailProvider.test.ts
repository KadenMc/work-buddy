import { describe, expect, it, vi } from "vitest";

import {
  InMemoryCoworkSittingTransport,
  type CoworkSittingTransport,
} from "../suggestions/sitting";
import type { SittingSubmission } from "../rail/provider";
import { LiveReviewRailProvider } from "./LiveReviewRailProvider";
import type { CoworkDocClient } from "./HttpCoworkDocClient";
import type { R2DocPayload, R2Proposal } from "./types";
import type { CoworkSittingWorkspace } from "./sittingWorkspace";

const producer = {
  model: "research-agent",
  model_source: "session-manifest",
  session_id: "sess-1",
  surface: "mcp",
} as const;

const proposal = (over: Partial<R2Proposal>): R2Proposal => ({
  proposal_id: "s1",
  kind: "edit",
  quote_anchor: { exact: "the cache key", prefix: "", suffix: "" },
  replacement: "the cache key and vault hash",
  rationale: "r",
  tldr: "t",
  producer,
  epistemic_state: "ai_proposed",
  base_doc_sha256: "base",
  canonical_sha256: "canon-s1",
  base_ok: true,
  status: "open",
  fixes_ref: null,
  claim_refs: [],
  created_at: "2026-07-17T12:00:00Z",
  ...over,
});

const payload = (proposals: readonly R2Proposal[]): R2DocPayload => ({
  document_id: "doc-1",
  store_id: "store-1",
  path: "docs/demo.md",
  title: "demo.md",
  profile: "co_authored",
  hashes: {
    ydoc_snapshot_sha256: null,
    last_materialized_sha256: "matsha",
    current_file_sha256: "filesha",
  },
  drift: { state: "clean", diff_available: false },
  open_proposals: proposals,
  expressions: [],
  provenance_spans: [],
  events_cursor: "c0",
});

const docClientReturning = (value: R2DocPayload): CoworkDocClient => ({
  fetchDoc: async () => value,
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
};

const workspaceRecording = () => {
  const events: string[] = [];
  const workspace: CoworkSittingWorkspace = {
    synchronize: async () => ({
      expectedFileSha256: "f".repeat(64),
      expectedStructuredHeadSha256: "h".repeat(64),
      generation: 1,
    }),
    prepare: async (items, generation) => {
      events.push(`prepared:${items.map((item) => item.proposal_id).join(",")}`);
      return {
        generation,
        commit: {
          snapshot: new Uint8Array([1]),
          snapshot_sha256: "s".repeat(64),
          rendered_markdown: "# materialized\n",
          rendered_sha256: "m".repeat(64),
        },
        dispose: () => undefined,
      };
    },
    isCurrent: () => true,
    refreshFromServer: async () => {
      events.push("refreshed");
    },
  };
  return { workspace, events };
};

const build = (options?: {
  readonly doc?: R2DocPayload;
  readonly getSittingWorkspace?: () => CoworkSittingWorkspace | null;
  readonly sittingTransport?: CoworkSittingTransport;
}) =>
  new LiveReviewRailProvider({
    docClient: docClientReturning(options?.doc ?? payload([proposal({})])),
    documentId: "doc-1",
    storeId: "store-1",
    sittingTransport: options?.sittingTransport ?? new InMemoryCoworkSittingTransport(),
    getSittingWorkspace:
      options?.getSittingWorkspace ?? (() => workspaceRecording().workspace),
  });

describe("LiveReviewRailProvider", () => {
  it("load returns the rail data from the R2 pull", async () => {
    const provider = build();
    const data = await provider.load();
    expect(data.title).toBe("demo.md");
    expect(data.proposals.map((p) => p.proposalId)).toEqual(["s1"]);
    expect(data.drift.currentFileSha256).toBe("filesha");
  });

  it("emits the same pull to the proposal and health channels", async () => {
    const provider = build({
      doc: payload([proposal({ proposal_id: "s1" }), proposal({ proposal_id: "s2" })]),
    });
    const proposals = vi.fn();
    const data = vi.fn();
    provider.onProposals(proposals);
    provider.onData(data);

    await provider.load();

    expect(proposals).toHaveBeenCalledTimes(1);
    expect(proposals.mock.calls[0][0].map((p: { proposal_id: string }) => p.proposal_id)).toEqual([
      "s1",
      "s2",
    ]);
    expect(data).toHaveBeenCalledTimes(1);
    expect(data.mock.calls[0][0].proposals.map((p: { proposalId: string }) => p.proposalId)).toEqual([
      "s1",
      "s2",
    ]);
  });

  it("replays the last pull to a late subscriber", async () => {
    const provider = build();
    await provider.load();
    const late = vi.fn();
    provider.onProposals(late);
    expect(late).toHaveBeenCalledTimes(1);
  });

  it("does not let an older overlapping pull overwrite a newer review snapshot", async () => {
    const older = deferred<R2DocPayload>();
    const newer = deferred<R2DocPayload>();
    const fetchDoc = vi
      .fn<() => Promise<R2DocPayload>>()
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    const provider = new LiveReviewRailProvider({
      docClient: { fetchDoc },
      documentId: "doc-1",
      storeId: "store-1",
      sittingTransport: new InMemoryCoworkSittingTransport(),
      getSittingWorkspace: () => workspaceRecording().workspace,
    });
    const proposals = vi.fn();
    provider.onProposals(proposals);

    const first = provider.load();
    const second = provider.load();
    newer.resolve(payload([proposal({ proposal_id: "newer" })]));
    await expect(second).resolves.toMatchObject({
      proposals: [expect.objectContaining({ proposalId: "newer" })],
    });
    older.resolve(payload([proposal({ proposal_id: "older" })]));
    await expect(first).resolves.toMatchObject({
      proposals: [expect.objectContaining({ proposalId: "newer" })],
    });

    expect(proposals).toHaveBeenCalledTimes(1);
    expect(proposals.mock.calls[0]?.[0]).toEqual([
      expect.objectContaining({ proposal_id: "newer" }),
    ]);
  });

  it("does not publish an older pull that resolves before the newer pull", async () => {
    const older = deferred<R2DocPayload>();
    const newer = deferred<R2DocPayload>();
    const fetchDoc = vi
      .fn<() => Promise<R2DocPayload>>()
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    const provider = new LiveReviewRailProvider({
      docClient: { fetchDoc },
      documentId: "doc-1",
      storeId: "store-1",
      sittingTransport: new InMemoryCoworkSittingTransport(),
      getSittingWorkspace: () => workspaceRecording().workspace,
    });
    const proposals = vi.fn();
    provider.onProposals(proposals);

    const first = provider.load();
    const second = provider.load();
    older.resolve(payload([proposal({ proposal_id: "older" })]));
    await Promise.resolve();
    await Promise.resolve();
    expect(proposals).not.toHaveBeenCalled();

    newer.resolve(payload([proposal({ proposal_id: "newer" })]));
    await expect(Promise.all([first, second])).resolves.toEqual([
      expect.objectContaining({
        proposals: [expect.objectContaining({ proposalId: "newer" })],
      }),
      expect.objectContaining({
        proposals: [expect.objectContaining({ proposalId: "newer" })],
      }),
    ]);
    expect(proposals).toHaveBeenCalledTimes(1);
  });

  it("fans an invalidation out to the rail's reload listeners", async () => {
    const provider = build();
    const reload = vi.fn();
    provider.subscribe(reload);
    provider.invalidate();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("submitSitting delegates through the prepared workspace", async () => {
    const { workspace, events } = workspaceRecording();
    const provider = build({ getSittingWorkspace: () => workspace });
    const submission: SittingSubmission = {
      baseDocSha256: "base-sha",
      proposalDecisions: [
        { proposalId: "s1", verb: "confirm", canonicalSha256: "canon-s1" },
      ],
      claimDecisions: [],
    };

    const result = await provider.submitSitting(submission);

    expect(events).toEqual(["prepared:s1", "refreshed"]);
    expect(result.results[0]?.result).toBe("applied");
  });

  it("throws when the editor workspace is not ready", async () => {
    const provider = build({ getSittingWorkspace: () => null });
    await expect(
      provider.submitSitting({
        baseDocSha256: "base-sha",
        proposalDecisions: [
          { proposalId: "s1", verb: "confirm", canonicalSha256: "canon-s1" },
        ],
        claimDecisions: [],
      }),
    ).rejects.toThrow(/editor is not ready/u);
  });

  it.each(["claim-only", "mixed"] as const)(
    "fails closed for %s live claim decisions before touching workspace or transport",
    async (shape) => {
      const getWorkspace = vi.fn(() => workspaceRecording().workspace);
      const transport: CoworkSittingTransport = {
        prepare: vi.fn(),
        commit: vi.fn(),
        cancel: vi.fn(),
      };
      const provider = build({
        getSittingWorkspace: getWorkspace,
        sittingTransport: transport,
      });
      await expect(
        provider.submitSitting({
          baseDocSha256: "base-sha",
          proposalDecisions:
            shape === "mixed"
              ? [{ proposalId: "s1", verb: "confirm", canonicalSha256: "canon-s1" }]
              : [],
          claimDecisions: [
            { claimId: "claim-1", verb: "confirm", canonicalSha256: "claim-sha" },
          ],
        }),
      ).rejects.toThrow(/No sitting decisions were submitted/u);
      expect(getWorkspace).not.toHaveBeenCalled();
      expect(transport.prepare).not.toHaveBeenCalled();
      expect(transport.commit).not.toHaveBeenCalled();
    },
  );
});
