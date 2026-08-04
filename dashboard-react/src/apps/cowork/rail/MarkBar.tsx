/**
 * The mark bar. It renders the verb set for the selected item and stages one
 * per-item decision (section 1.5). Edit proposals get the seven edit verbs,
 * flags get Endorse / Dismiss / Redirect, claims get the six committed claim
 * verbs. When an edit's original passage cannot be placed, only Accept and
 * Amend are disabled with a stated reason. Verbs that
 * need a replacement, a redirect note, or a verbatim negation collect it inline
 * before staging (S3), so a durable decision is never minted from a mis-click.
 */

import { useCallback, useEffect, useId, useRef, useState } from "react";

import {
  DEFAULT_COWORK_SHORTCUT_BINDINGS,
  type CoworkShortcutBindings,
} from "../keyboard";
import {
  formatShortcutChord,
  shortcutAriaValue,
  shortcutMatchesEvent,
  shouldIgnoreShortcutEvent,
} from "../../../settings/keybindings";
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
  /** Freeze every staging control while Review is applying a confirmed request. */
  readonly disabled?: boolean;
  /** Show the single-key hint on each verb (queue mode). */
  readonly showHotkeys?: boolean;
  /** Effective user bindings shared by Queue navigation and decision verbs. */
  readonly bindings?: CoworkShortcutBindings;
  /** Whether this visible Queue surface may handle its window-level shortcuts. */
  readonly keyboardShortcutsEnabled?: boolean;
}

/** The inline-input label for each verb that collects one before staging. */
const INPUT_LABEL: Partial<Record<ProposalVerbKind, string>> = {
  edit_confirm: "Your replacement",
  redirect: "Guidance for the agent",
  reject_as_false: "The correct statement, recorded as a negation",
  reject_as_preference: "Your preferred phrasing, recorded as a preference",
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
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fieldId = useId();
  const bindings = props.bindings ?? DEFAULT_COWORK_SHORTCUT_BINDINGS;
  const targetIdentity =
    target.kind === "proposal"
      ? `proposal:${target.proposal.proposalId}:${target.proposal.canonicalSha256}`
      : `claim:${target.claim.claimId}:${target.claim.canonicalSha256}`;

  useEffect(() => {
    setInputVerb(null);
    setInputValue("");
  }, [targetIdentity]);

  useEffect(() => {
    if (inputVerb !== null) inputRef.current?.focus();
  }, [inputVerb]);

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

  const openInput = useCallback((verb: ProposalVerbKind, prefill: string) => {
    if (props.disabled) return;
    setInputVerb(verb);
    setInputValue(prefill);
  }, [props.disabled]);

  const cancelInput = useCallback(() => {
    if (props.disabled) return;
    setInputVerb(null);
    setInputValue("");
  }, [props.disabled]);

  const commitProposalVerb = useCallback((proposal: ReviewProposal, verb: ProposalVerbKind) => {
    if (props.disabled) return;
    const needsAmend = verb === "edit_confirm";
    const needsRedirect = verb === "redirect";
    const needsNegation =
      verb === "reject_as_false" && rejectAsFalseNeedsNegation(proposal);
    const needsPreference = verb === "reject_as_preference";

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
    if (needsPreference) {
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
  }, [
    openInput,
    props.disabled,
    props.onClearProposal,
    props.onStageProposal,
    stagedVerb,
  ]);

  const submitInput = (proposal: ReviewProposal) => {
    if (props.disabled) return;
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
      ...(inputVerb === "reject_as_preference" ? { preferenceText: trimmed } : {}),
    };
    props.onStageProposal(decision);
    cancelInput();
  };

  const commitClaimVerb = useCallback((claim: ReviewClaim, verb: ClaimVerbKind) => {
    if (props.disabled) return;
    if (stagedVerb === verb) {
      props.onClearClaim(claim.claimId);
      return;
    }
    props.onStageClaim({
      claimId: claim.claimId,
      verb,
      canonicalSha256: claim.canonicalSha256,
    });
  }, [
    props.disabled,
    props.onClearClaim,
    props.onStageClaim,
    stagedVerb,
  ]);

  const targetUnavailable =
    target.kind === "proposal" &&
    ((target.proposal.applicability !== undefined &&
      target.proposal.applicability.status !== "applicable") ||
      (target.proposal.applicability === undefined && !target.proposal.baseOk));

  useEffect(() => {
    if (!(props.keyboardShortcutsEnabled ?? false) || inputVerb !== null) {
      return undefined;
    }
    const handler = (event: KeyboardEvent) => {
      if (event.repeat || shouldIgnoreShortcutEvent(event)) return;
      const options =
        target.kind === "proposal"
          ? verbsForProposal(target.proposal.kind)
          : CLAIM_VERBS;
      for (const option of options) {
        if (option.shortcut === undefined) continue;
        if (!shortcutMatchesEvent(bindings[option.shortcut], event)) continue;
        if (
          props.disabled ||
          (target.kind === "proposal" &&
            !isVerbDecidable(target.proposal, option.verb as ProposalVerbKind))
        ) {
          return;
        }
        event.preventDefault();
        if (target.kind === "proposal") {
          commitProposalVerb(target.proposal, option.verb as ProposalVerbKind);
        } else {
          commitClaimVerb(target.claim, option.verb as ClaimVerbKind);
        }
        return;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [
    bindings,
    commitClaimVerb,
    commitProposalVerb,
    inputVerb,
    props.disabled,
    props.keyboardShortcutsEnabled,
    target,
  ]);

  return (
    <section
      className="wb-cowork-rail__markbar"
      aria-label="Decide"
      aria-busy={props.disabled || undefined}
    >
      <p className="wb-cowork-rail__markbar-ctx">
        <span className="wb-cowork-rail__markbar-sel">{contextLabel}</span>
        <span className="wb-cowork-rail__markbar-hash" aria-label="Content hash">
          {shortHash(hashLabel)}
        </span>
      </p>

      {targetUnavailable ? (
        <p className="wb-cowork-rail__stale-note" role="status">
          The original passage cannot be placed safely. Accept and Amend are
          unavailable; other review decisions still work.
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
                    disabled={
                      (props.disabled ?? false) ||
                      !isVerbDecidable(target.proposal, entry.verb)
                    }
                    staged={stagedVerb === entry.verb}
                    showHotkey={props.showHotkeys ?? false}
                    shortcut={
                      entry.shortcut === undefined
                        ? undefined
                        : bindings[entry.shortcut]
                    }
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
                  disabled={props.disabled ?? false}
                  staged={stagedVerb === entry.verb}
                  showHotkey={props.showHotkeys ?? false}
                  shortcut={
                    entry.shortcut === undefined
                      ? undefined
                      : bindings[entry.shortcut]
                  }
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
            ref={inputRef}
            id={fieldId}
            className="wb-cowork-rail__verb-input-field"
            value={inputValue}
            rows={3}
            disabled={props.disabled}
            onChange={(event) => setInputValue(event.target.value)}
          />
          <div className="wb-cowork-rail__verb-input-actions">
            <button
              type="submit"
              className="wb-cowork-rail__verb wb-cowork-rail__verb--primary"
              disabled={props.disabled}
            >
              Stage
            </button>
            <button
              type="button"
              className="wb-cowork-rail__verb wb-cowork-rail__verb--neutral"
              disabled={props.disabled}
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
  readonly shortcut?: string;
  onClick(): void;
}

function VerbButton<Verb extends string>({
  option,
  disabled,
  staged,
  showHotkey,
  shortcut,
  onClick,
}: VerbButtonProps<Verb>) {
  return (
    <button
      type="button"
      className={`${toneClass(option.tone)}${staged ? " is-staged" : ""}`}
      disabled={disabled}
      aria-pressed={staged}
      aria-keyshortcuts={
        showHotkey && shortcut !== undefined
          ? shortcutAriaValue(shortcut)
          : undefined
      }
      onClick={onClick}
    >
      {option.label}
      {showHotkey && shortcut !== undefined ? (
        <span className="wb-cowork-rail__verb-key" aria-hidden="true">
          {formatShortcutChord(shortcut)}
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
