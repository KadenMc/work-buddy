/**
 * The mark-bar verb vocabulary. C1 surface contract section 1.5 maps the UI
 * labels to the shipped kernel gesture-kind names exactly once, and this module
 * is the single realization of that table on the client.
 */

import type {
  ProposalKind,
  ProposalVerbKind,
  ReviewProposal,
} from "./contracts";
import type { CoworkShortcutCommandId } from "../keyboard";

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
  /** The user-configurable decision shortcut, when this verb has one. */
  readonly shortcut?: Extract<
    CoworkShortcutCommandId,
    "accept" | "amend" | "reject" | "defer"
  >;
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
  { label: "Accept", verb: "confirm", tone: "primary", shortcut: "accept", input: "none" },
  { label: "Amend", verb: "edit_confirm", tone: "neutral", shortcut: "amend", input: "amend" },
  { label: "Reject", verb: "reject_plain", tone: "danger", shortcut: "reject", input: "none" },
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
  { label: "Defer", verb: "defer", tone: "neutral", shortcut: "defer", input: "none" },
];

/** Flag verbs (PRD section 6, flag row): Endorse, Dismiss, Redirect. */
export const FLAG_VERBS: readonly VerbOption<ProposalVerbKind>[] = [
  { label: "Endorse", verb: "endorse", tone: "primary", shortcut: "accept", input: "none" },
  { label: "Dismiss", verb: "dismiss", tone: "danger", shortcut: "reject", input: "none" },
  { label: "Redirect", verb: "redirect", tone: "neutral", input: "redirect_note" },
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

/**
 * Only Accept and Amend mutate document text, so only those verbs require a
 * safely located current target. Routing and flag decisions remain valid even
 * when the original passage moved or disappeared.
 */
const TARGET_INDEPENDENT: ReadonlySet<ProposalVerbKind> = new Set([
  "reject_plain",
  "reject_as_false",
  "reject_as_preference",
  "defer",
  "dismiss",
  "redirect",
  "endorse",
]);

/**
 * Whether a verb is decidable for a given proposal. New servers expose a typed
 * target assessment; baseOk is retained only for rolling compatibility.
 */
export function isVerbDecidable(
  proposal: Pick<ReviewProposal, "applicability" | "baseOk">,
  verb: ProposalVerbKind,
): boolean {
  if (TARGET_INDEPENDENT.has(verb)) return true;
  return proposal.applicability?.status === "applicable" ||
    (proposal.applicability === undefined && proposal.baseOk);
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
