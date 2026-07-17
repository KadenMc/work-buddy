/**
 * The client-side Yjs transport seam for a Co-work document (R3 pull / R4 push, section
 * 1.4, amended R3/R4 per C3). v1 has NO server-side Yjs runtime: the server stores
 * OPAQUE blobs (a compacted snapshot plus appended update batches) and content hashes,
 * and never merges, diffs, or constructs Yjs state. This interface is exactly that
 * contract, so a real same-origin HTTP transport and the in-memory test double are
 * interchangeable behind it. No live HTTP is wired this wave.
 */

/** The result of an R3 pull. A full pull leads with the compacted snapshot blob. */
export interface CoworkYdocPull {
  /** The latest compacted snapshot blob, present only on a full pull (no offset). */
  readonly snapshot: Uint8Array | null;
  /** Content hash of the snapshot blob when one leads the body (`X-WB-Snapshot-Sha256`). */
  readonly snapshotSha256: string | null;
  /** Update batches appended after the snapshot (full pull) or after the offset (slice). */
  readonly batches: readonly Uint8Array[];
  /** Latest content hash (`X-WB-Doc-Sha256`), the optimistic-concurrency base for a push. */
  readonly docSha256: string;
  /** Opaque cursor for the next pull (`X-WB-Next-Offset`). */
  readonly nextOffset: string;
}

export interface CoworkYdocPullRequest {
  /** Absent asks for snapshot plus all following batches; present slices batches only. */
  readonly sinceOffset?: string;
}

/** A client-driven compaction rider on a push (`X-WB-Compacted-Snapshot-Sha256`). */
export interface CoworkYdocCompaction {
  readonly snapshot: Uint8Array;
  readonly snapshotSha256: string;
}

export interface CoworkYdocPushRequest {
  /** One opaque Yjs update batch (human direct edits only, section 1.4). */
  readonly batch: Uint8Array;
  /** The content hash the client based this batch on (`X-WB-Base-Sha256`). */
  readonly baseSha256: string;
  /** Present when the client has just compacted; the server content-addresses the blob. */
  readonly compaction?: CoworkYdocCompaction;
}

export type CoworkYdocPushResult =
  | {
      readonly ok: true;
      readonly applied: boolean;
      readonly docSha256: string;
      readonly nextOffset: string;
    }
  | {
      readonly ok: false;
      readonly error: "stale_base";
      readonly serverDocSha256: string;
    };

export interface CoworkYdocTransport {
  /** R3 pull: opaque snapshot plus batches, or an offset-sliced batch tail. Idempotent. */
  pull(request: CoworkYdocPullRequest): Promise<CoworkYdocPull>;
  /** R4 push: append one opaque batch, optionally riding a compacted snapshot. Idempotent. */
  push(request: CoworkYdocPushRequest): Promise<CoworkYdocPushResult>;
}
