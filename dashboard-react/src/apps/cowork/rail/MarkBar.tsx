/**
 * The mark bar. It renders the verb set for the selected item and stages one
 * per-item decision (section 1.5). Edit proposals get the seven edit verbs,
 * flags get Endorse / Dismiss / Redirect, claims get the six committed claim
 * verbs. On a stale-base proposal only the reject family and Defer are
 * decidable, and the rest are disabled with a stated reason (S6). Verbs that
 * need a replacement, a redirect note, or a verbatim negation collect it inline
 * before staging (S3), so a durable decision is never minted from a mis-click.
 */

import { useId, useState } from "react";

import type {
  ClaimVerbKind,
  ProposalVerbKind,
  ReviewClaim,
  ReviewProposal,
  StagedClaimDecision,
  StagedDecision,
} from "./contracts";
import {
  CLAIM_VERBS,
  isVerbDecidable,
  rejectAsFalseNeedsNegation,
  verbsForProposal,
  type VerbOption,
  type VerbTone,
} from "./verbs";

export type MarkBarTarget =
  | { readonly kind: "proposal"; readonly proposal: ReviewProposal }
  | { readonly kind: "claim"; readonly claim: ReviewClaim };

export interface MarkBarProps {
  readonly target: MarkBarTarget;
  readonly stagedProposal?: StagedDecision;
  readonly stagedClaim?: StagedClaimDecision;
  onStageProposal(decision: StagedDecision): void;
  onStageClaim(decision: StagedClaimDecision): void;
  onClearProposal(proposalId: string): void;
  onClearClaim(claimId: string): void;
  /** Show the single-key hint on each verb (queue mode). */
  readonly showHotkeys?: boolean;
}

/** The inline-input label for each verb that collects one before staging. */
const INPUT_LABEL: Partial<Record<ProposalVerbKind, string>> = {
  edit_confirm: "Your replacement",
  redirect: "Guidance for the agent",
  reject_as_false: "The correct statement, recorded as a negation",
};

function toneClass(tone: VerbTone): string {
  return `wb-cowork-rail__verb wb-cowork-rail__verb--${tone}`;
}

/** Render a verb row with a divider inserted at each tone boundary. */
function withSeparators<Verb extends string>(
  verbs: readonly VerbOption<Verb>[],
): readonly (VerbOption<Verb> | "sep")[] {
  const out: (VerbOption<Verb> | "sep")[] = [];
  let previousTone: VerbTone | null = null;
  for (const verb of verbs) {
    if (previousTone !== null && previousTone !== verb.tone) out.push("sep");
    out.push(verb);
    previousTone = verb.tone;
  }
  return out;
}

export function MarkBar(props: MarkBarProps) {
  const { target } = props;
  const [inputVerb, setInputVerb] = useState<ProposalVerbKind | null>(null);
  const [inputValue, setInputValue] = useState("");
  const fieldId = useId();

  const contextLabel =
    target.kind === "proposal"
      ? `${target.proposal.kind === "flag" ? "Flag" : verbNoun(target.proposal)}, "${target.proposal.tldr}"`
      : `Claim, "${truncate(target.claim.proposition)}"`;
  const hashLabel =
    target.kind === "proposal"
      ? target.proposal.canonicalSha256
      : target.claim.canonicalSha256;

  const stagedVerb =
    target.kind === "proposal"
      ? props.stagedProposal?.verb
      : props.stagedClaim?.verb;

  const openInput = (verb: ProposalVerbKind, prefill: string) => {
    setInputVerb(verb);
    setInputValue(prefill);
  };

  const cancelInput = () => {
    setInputVerb(null);
    setInputValue("");
  };

  const commitProposalVerb = (proposal: ReviewProposal, verb: ProposalVerbKind) => {
    const needsAmend = verb === "edit_confirm";
    const needsRedirect = verb === "redirect";
    const needsNegation =
      verb === "reject_as_false" && rejectAsFalseNeedsNegation(proposal);

    if (needsAmend) {
      openInput(verb, proposal.replacement ?? "");
      return;
    }
    if (needsRedirect) {
      openInput(verb, "");
      return;
    }
    if (needsNegation) {
      openInput(verb, "");
      return;
    }

    // A no-input verb toggles: click the staged verb again to clear it.
    if (stagedVerb === verb) {
      props.onClearProposal(proposal.proposalId);
      return;
    }
    props.onStageProposal({
      proposalId: proposal.proposalId,
      verb,
      canonicalSha256: proposal.canonicalSha256,
    });
  };

  const submitInput = (proposal: ReviewProposal) => {
    if (inputVerb === null) return;
    const trimmed = inputValue.trim();
    if (inputVerb !== "edit_confirm" && trimmed.length === 0) return;
    const decision: StagedDecision = {
      proposalId: proposal.proposalId,
      verb: inputVerb,
      canonicalSha256: proposal.canonicalSha256,
      ...(inputVerb === "edit_confirm" ? { amendContent: inputValue } : {}),
      ...(inputVerb === "redirect" ? { redirectNote: trimmed } : {}),
      ...(inputVerb === "reject_as_false" ? { negationText: trimmed } : {}),
    };
    props.onStageProposal(decision);
    cancelInput();
  };

  const commitClaimVerb = (claim: ReviewClaim, verb: ClaimVerbKind) => {
    if (stagedVerb === verb) {
      props.onClearClaim(claim.claimId);
      return;
    }
    props.onStageClaim({
      claimId: claim.claimId,
      verb,
      canonicalSha256: claim.canonicalSha256,
    });
  };

  const staleBase = target.kind === "proposal" && !target.proposal.baseOk;

  return (
    <section className="wb-cowork-rail__markbar" aria-label="Decide">
      <p className="wb-cowork-rail__markbar-ctx">
        <span className="wb-cowork-rail__markbar-sel">{contextLabel}</span>
        <span className="wb-cowork-rail__markbar-hash" aria-label="Content hash">
          {shortHash(hashLabel)}
        </span>
      </p>

      {staleBase ? (
        <p className="wb-cowork-rail__stale-note" role="status">
          Stale base. The document changed since this was proposed, so it can be
          rejected or deferred only.
        </p>
      ) : null}

      <div className="wb-cowork-rail__verbs" role="group" aria-label="Verbs">
        {target.kind === "proposal"
          ? withSeparators(verbsForProposal(target.proposal.kind)).map(
              (entry, index) =>
                entry === "sep" ? (
                  <span
                    key={`sep-${index}`}
                    className="wb-cowork-rail__verb-sep"
                    aria-hidden="true"
                  />
                ) : (
                  <VerbButton
                    key={entry.verb + entry.label}
                    option={entry}
                    disabled={!isVerbDecidable(target.proposal, entry.verb)}
                    staged={stagedVerb === entry.verb}
                    showHotkey={props.showHotkeys ?? false}
                    onClick={() =>
                      commitProposalVerb(target.proposal, entry.verb)
                    }
                  />
                ),
            )
          : withSeparators(CLAIM_VERBS).map((entry, index) =>
              entry === "sep" ? (
                <span
                  key={`sep-${index}`}
                  className="wb-cowork-rail__verb-sep"
                  aria-hidden="true"
                />
              ) : (
                <VerbButton
                  key={entry.verb + entry.label}
                  option={entry}
                  disabled={false}
                  staged={stagedVerb === entry.verb}
                  showHotkey={props.showHotkeys ?? false}
                  onClick={() => commitClaimVerb(target.claim, entry.verb)}
                />
              ),
            )}
      </div>

      {inputVerb !== null && target.kind === "proposal" ? (
        <form
          className="wb-cowork-rail__verb-input"
          onSubmit={(event) => {
            event.preventDefault();
            submitInput(target.proposal);
          }}
        >
          <label className="wb-cowork-rail__verb-input-label" htmlFor={fieldId}>
            {INPUT_LABEL[inputVerb] ?? "Details"}
          </label>
          <textarea
            id={fieldId}
            className="wb-cowork-rail__verb-input-field"
            value={inputValue}
            rows={3}
            onChange={(event) => setInputValue(event.target.value)}
          />
          <div className="wb-cowork-rail__verb-input-actions">
            <button
              type="submit"
              className="wb-cowork-rail__verb wb-cowork-rail__verb--primary"
            >
              Stage
            </button>
            <button
              type="button"
              className="wb-cowork-rail__verb wb-cowork-rail__verb--neutral"
              onClick={cancelInput}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : null}
    </section>
  );
}

interface VerbButtonProps<Verb extends string> {
  readonly option: VerbOption<Verb>;
  readonly disabled: boolean;
  readonly staged: boolean;
  readonly showHotkey: boolean;
  onClick(): void;
}

function VerbButton<Verb extends string>({
  option,
  disabled,
  staged,
  showHotkey,
  onClick,
}: VerbButtonProps<Verb>) {
  return (
    <button
      type="button"
      className={`${toneClass(option.tone)}${staged ? " is-staged" : ""}`}
      disabled={disabled}
      aria-pressed={staged}
      onClick={onClick}
    >
      {option.label}
      {showHotkey && option.hotkey !== undefined ? (
        <span className="wb-cowork-rail__verb-key" aria-hidden="true">
          {option.hotkey}
        </span>
      ) : null}
    </button>
  );
}

function verbNoun(proposal: ReviewProposal): string {
  if (proposal.changeType === "deletion") return "Deletion";
  if (proposal.changeType === "modification") return "Modification";
  return "Insertion";
}

function truncate(text: string, max = 48): string {
  return text.length > max ? `${text.slice(0, max - 1)}...` : text;
}

function shortHash(hash: string): string {
  return `#${hash.slice(0, 4)}`;
}
