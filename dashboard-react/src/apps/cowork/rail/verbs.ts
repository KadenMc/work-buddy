/**
 * The mark-bar verb vocabulary. C1 surface contract section 1.5 maps the UI
 * labels to the shipped kernel gesture-kind names exactly once, and this module
 * is the single realization of that table on the client. The six claim verbs
 * come from the kernel truth_claim_* capabilities (propose, confirm, reject,
 * challenge, supersede, redact). No new kind is coined here.
 */

import type {
  ClaimVerbKind,
  ProposalKind,
  ProposalVerbKind,
  ReviewProposal,
} from "./contracts";

/** Visual weight for a verb button, so the danger reject family reads apart. */
export type VerbTone = "primary" | "neutral" | "danger";

/** The extra input a verb needs before it can be staged (section 1.5). */
export type VerbInput =
  | "none"
  | "amend"
  | "redirect_note"
  | "negation_text"
  | "preference_text";

/** One selectable verb on the mark bar. */
export interface VerbOption<Verb extends string> {
  /** The UI label the human reads (section 1.5 left column). */
  readonly label: string;
  /** The wire gesture-kind name submitted to R5 (section 1.5 right column). */
  readonly verb: Verb;
  readonly tone: VerbTone;
  /** A single-key hint shown in the queue mode, when the verb has one. */
  readonly hotkey?: string;
  /** The extra input this verb collects before staging. */
  readonly input: VerbInput;
}

/**
 * Edit-proposal verbs (section 1.5). Accept and Amend apply, the three reject
 * classes and Defer keep or close, Redirect leaves the proposal open with a
 * typed note. Reject as false collects verbatim negation only when the
 * proposal carries no claim_refs (S3), decided per proposal at stage time.
 */
export const EDIT_VERBS: readonly VerbOption<ProposalVerbKind>[] = [
  { label: "Accept", verb: "confirm", tone: "primary", hotkey: "a", input: "none" },
  { label: "Amend", verb: "edit_confirm", tone: "neutral", hotkey: "e", input: "amend" },
  { label: "Reject", verb: "reject_plain", tone: "danger", hotkey: "x", input: "none" },
  {
    label: "Reject as false",
    verb: "reject_as_false",
    tone: "danger",
    input: "negation_text",
  },
  {
    label: "Reject as preference",
    verb: "reject_as_preference",
    tone: "danger",
    input: "preference_text",
  },
  { label: "Redirect", verb: "redirect", tone: "neutral", input: "redirect_note" },
  { label: "Defer", verb: "defer", tone: "neutral", hotkey: ".", input: "none" },
];

/** Flag verbs (PRD section 6, flag row): Endorse, Dismiss, Redirect. */
export const FLAG_VERBS: readonly VerbOption<ProposalVerbKind>[] = [
  { label: "Endorse", verb: "endorse", tone: "primary", hotkey: "a", input: "none" },
  { label: "Dismiss", verb: "dismiss", tone: "danger", hotkey: "x", input: "none" },
  { label: "Redirect", verb: "redirect", tone: "neutral", input: "redirect_note" },
];

/** The six committed claim verbs (kernel truth_claim_* capabilities). */
export const CLAIM_VERBS: readonly VerbOption<ClaimVerbKind>[] = [
  { label: "Confirm", verb: "confirm", tone: "primary", hotkey: "a", input: "none" },
  { label: "Reject", verb: "reject", tone: "danger", hotkey: "x", input: "none" },
  { label: "Challenge", verb: "challenge", tone: "neutral", input: "none" },
  { label: "Supersede", verb: "supersede", tone: "neutral", input: "none" },
  { label: "Redact", verb: "redact", tone: "danger", input: "none" },
  { label: "Propose", verb: "propose", tone: "neutral", input: "none" },
];

/** The verb list for a proposal or flag card. */
export function verbsForProposal(
  kind: ProposalKind,
): readonly VerbOption<ProposalVerbKind>[] {
  return kind === "flag" ? FLAG_VERBS : EDIT_VERBS;
}

/** UI label for a staged proposal or flag verb (section 1.5 left column). */
export const PROPOSAL_VERB_LABEL: Record<ProposalVerbKind, string> = {
  confirm: "Accept",
  edit_confirm: "Amend",
  reject_plain: "Reject",
  reject_as_false: "Reject as false",
  reject_as_preference: "Reject as preference",
  redirect: "Redirect",
  defer: "Defer",
  endorse: "Endorse",
  dismiss: "Dismiss",
};

/** UI label for a staged claim verb (the six committed claim verbs). */
export const CLAIM_VERB_LABEL: Record<ClaimVerbKind, string> = {
  propose: "Propose",
  confirm: "Confirm",
  reject: "Reject",
  challenge: "Challenge",
  supersede: "Supersede",
  redact: "Redact",
};

/**
 * The reject and defer verbs that stay decidable on a stale-base proposal (S6).
 * confirm, edit_confirm, redirect, and endorse are structurally undecidable
 * against a stale base (they would return an error with reason stale_base), so
 * the mark bar disables them.
 */
const STALE_BASE_ALLOWED: ReadonlySet<ProposalVerbKind> = new Set([
  "reject_plain",
  "reject_as_false",
  "reject_as_preference",
  "defer",
  "dismiss",
]);

/**
 * Whether a verb is decidable for a given proposal. On a stale base only the
 * reject family and defer or dismiss remain available (section 1.5 stale gate).
 */
export function isVerbDecidable(
  proposal: Pick<ReviewProposal, "baseOk">,
  verb: ProposalVerbKind,
): boolean {
  if (proposal.baseOk) return true;
  return STALE_BASE_ALLOWED.has(verb);
}

/**
 * Whether reject_as_false needs a verbatim negation from the human. It does
 * exactly when the proposal carries no claim_refs, otherwise the deterministic
 * negation of the referenced claim is minted server-side (S3).
 */
export function rejectAsFalseNeedsNegation(
  proposal: Pick<ReviewProposal, "claimRefs">,
): boolean {
  return proposal.claimRefs.length === 0;
}
