/**
 * Coarse, JSON-compatible document session carried through the ViewProvider snapshot
 * (section 5.2). The live binary Y.Doc, editor instance, decoration geometry, and the
 * staged sitting are widget-local state and never ride a snapshot. The Yjs binary and
 * the R5 sitting take the direct route to `/api/truth/doc/*`, not the provider.
 */

export type CoworkDriftState = "clean" | "drifted" | "missing";

export interface CoworkDocumentSummary {
  readonly documentId: string;
  readonly path: string;
  readonly title: string;
  readonly profile: string;
  readonly driftState: CoworkDriftState;
  readonly openProposalCount: number;
  readonly openFlagCount: number;
}

/** The coarse model the Co-work view provider delivers: which document is open. */
export interface CoworkViewModel {
  readonly document: CoworkDocumentSummary | null;
}

/**
 * The JSON-compatible input the coarse provider hydrates into the composite workspace
 * widget (the durable card). It carries which document is open and the session quality
 * the widget resolves its demo / live / empty mode from. The live Y.Doc, the editor
 * instance, and the staged sitting stay widget-local and never ride this input. The
 * widget reads them through the direct route seam its durable exemption sanctions.
 */
export interface CoworkWorkspaceInput {
  readonly document: CoworkDocumentSummary | null;
  readonly sessionQuality: string;
}
