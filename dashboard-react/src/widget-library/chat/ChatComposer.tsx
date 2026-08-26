import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  type Ref,
} from "react";
import { TextArea, TextField } from "react-aria-components";

import { HelpTarget, type HelpContent } from "../../dashboard/help";
import { Button, InlineAlert, Spinner } from "../../ui";
import { ChatExecutionPicker } from "./ChatExecutionPicker";
import type { ChatExecutionControl } from "./useChatExecutionProfile";
import "./styles.css";

export interface ChatComposerPrimaryAction {
  readonly label: string;
  /** Host-owned lock, independent of the textbox's disabled state. */
  readonly disabled: boolean;
  readonly pending?: boolean;
  readonly pendingLabel?: string;
  readonly buttonRef?: Ref<HTMLButtonElement>;
  readonly help?: HelpContent;
  /**
   * Called synchronously on explicit activation, without consuming the draft
   * or sending a message. The host owns errors, retries, and lifecycle focus.
   */
  onAction(): void | Promise<void>;
}

export interface ChatComposerProps {
  /**
   * Send intent. May return a promise. A resolved promise clears the draft, a
   * rejected one retains it so the human never loses typed text.
   */
  onSend(value: string): void | Promise<void>;
  /** Fully disable input, e.g. a stopped agent or a closed conversation. */
  readonly disabled?: boolean;
  /** Externally-driven pending state (the provider send is in flight). */
  readonly sending?: boolean;
  /**
   * Prevent another submission while preserving the textbox for drafting the
   * next turn, e.g. while an acknowledged message awaits its reply.
   */
  readonly submissionDisabled?: boolean;
  readonly placeholder?: string;
  /** Accessible label for the input. Visually hidden by default. */
  readonly label?: string;
  /** Inline error from the most recent failed send. */
  readonly errorMessage?: string;
  /** Seed the draft once on mount, e.g. from a retained unsent draft. */
  readonly initialValue?: string;
  /**
   * Observe the live draft: fired on every edit and with an empty string after
   * a successful send. A host uses this to retain the unsent draft and to arm
   * an unsaved-work guard. The composer still owns the text state.
   */
  onDraftChange?(value: string): void;
  /** Optional server-authoritative provider/model selection for this chat. */
  readonly execution?: ChatExecutionControl;
  /** Separate model lock before a host's explicit Start; read-only and in-flight work still win. */
  readonly executionDisabled?: boolean;
  /** Additive host context controls rendered above the shared input. */
  readonly accessory?: ReactNode;
  /** Compact host context rendered in the composer footer. */
  readonly footerAccessory?: ReactNode;
  /** Replace Send with an explicit host action; Enter in the textbox only edits. */
  readonly primaryAction?: ChatComposerPrimaryAction;
}

export function ChatComposer({
  onSend,
  disabled = false,
  sending = false,
  submissionDisabled = false,
  placeholder = "Type a message…",
  label = "Message",
  errorMessage,
  initialValue = "",
  onDraftChange,
  execution,
  executionDisabled,
  accessory,
  footerAccessory,
  primaryAction,
}: ChatComposerProps) {
  const [draft, setDraft] = useState(initialValue);
  const [busy, setBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const submittingRef = useRef(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const isSending = sending || busy;
  const isActionPending = actionBusy || primaryAction?.pending === true;
  const executionReadOnly = execution?.snapshot?.readOnly === true;
  const effectiveDisabled =
    disabled || executionReadOnly;
  const pickerDisabled =
    executionReadOnly ||
    (executionDisabled ?? disabled) ||
    isSending ||
    isActionPending ||
    submissionDisabled;
  const executionBlocksSend =
    execution?.selecting === true;
  const canSend =
    primaryAction === undefined &&
    !isActionPending &&
    !effectiveDisabled &&
    !isSending &&
    !submissionDisabled &&
    !executionBlocksSend &&
    draft.trim().length > 0;
  const canRunPrimaryAction =
    primaryAction !== undefined &&
    !primaryAction.disabled &&
    !isActionPending &&
    !executionReadOnly &&
    !isSending &&
    !submissionDisabled &&
    !executionBlocksSend;

  const grow = useCallback((element: HTMLTextAreaElement | null) => {
    if (element === null) return;
    element.style.height = "auto";
    element.style.overflowY = "hidden";
    const styles = globalThis.getComputedStyle(element);
    const borderHeight =
      (Number.parseFloat(styles.borderTopWidth) || 0) +
      (Number.parseFloat(styles.borderBottomWidth) || 0);
    const desiredHeight = element.scrollHeight + borderHeight;
    const parsedMaximum = Number.parseFloat(styles.maxHeight);
    const maximumHeight = Number.isFinite(parsedMaximum)
      ? parsedMaximum
      : Number.POSITIVE_INFINITY;
    const nextHeight = Math.min(desiredHeight, maximumHeight);
    if (nextHeight > 0) element.style.height = `${nextHeight}px`;
    element.style.overflowY =
      desiredHeight > maximumHeight ? "auto" : "hidden";
  }, []);

  useLayoutEffect(() => {
    grow(inputRef.current);
  }, [draft, grow]);

  useLayoutEffect(() => {
    const element = inputRef.current;
    if (element === null || typeof ResizeObserver === "undefined") return;
    let width = element.getBoundingClientRect().width;
    const observer = new ResizeObserver((entries) => {
      const nextWidth = entries[0]?.contentRect.width ?? width;
      if (nextWidth === width) return;
      width = nextWidth;
      grow(element);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [grow]);

  const submit = async () => {
    const value = draft.trim();
    if (
      primaryAction !== undefined ||
      isActionPending ||
      value.length === 0 ||
      effectiveDisabled ||
      isSending ||
      submittingRef.current ||
      submissionDisabled ||
      executionBlocksSend
    ) {
      return;
    }
    // React state does not update synchronously. Guard the imperative submit
    // path too, so Enter plus click (or two submit events in one tick) cannot
    // dispatch the same draft twice before the disabled state renders.
    submittingRef.current = true;
    setBusy(true);
    try {
      await onSend(value);
      setDraft("");
      onDraftChange?.("");
      // A host may remain editable while a turn is in flight. Restore the
      // composer only while focus is still inside its own form; a delayed
      // acknowledgement must never take focus back from a user's next action.
      if (inputRef.current?.form?.contains(document.activeElement)) {
        inputRef.current.focus();
      }
    } catch {
      // Retain the draft. The panel surfaces the failure through errorMessage.
    } finally {
      submittingRef.current = false;
      setBusy(false);
    }
  };

  const runPrimaryAction = async () => {
    if (
      primaryAction === undefined ||
      !canRunPrimaryAction ||
      submittingRef.current
    ) {
      return;
    }
    submittingRef.current = true;
    setActionBusy(true);
    try {
      // Invoke before the first await: hosts can freeze the context authorized
      // by this activation. This path never enters the message-send protocol.
      await primaryAction.onAction();
    } catch {
      // The host surfaces its error and decides whether to retry the exact
      // action. Success and failure both leave the unsent draft untouched.
    } finally {
      submittingRef.current = false;
      setActionBusy(false);
    }
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // A launch/host action is authorized only by activating its own button.
    // Keep normal textarea editing, including Enter, while it replaces Send.
    if (primaryAction !== undefined) return;
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  return (
    <form className="wb-chat-composer" onSubmit={handleSubmit}>
      {errorMessage !== undefined ? (
        <InlineAlert tone="danger" role="status" className="wb-chat-composer__error">
          {errorMessage}
        </InlineAlert>
      ) : null}
      {accessory}
      <div className="wb-chat-composer__shell">
        <TextField
          className="wb-chat-composer__field"
          aria-label={label}
          value={draft}
          isDisabled={effectiveDisabled}
          onChange={(value) => {
            setDraft(value);
            onDraftChange?.(value);
          }}
        >
          <TextArea
            ref={inputRef}
            className="wb-chat-composer__input"
            rows={1}
            placeholder={placeholder}
            onKeyDown={handleKeyDown}
          />
        </TextField>
        <div className="wb-chat-composer__footer">
          {footerAccessory}
          {execution === undefined ? (
            <span className="wb-chat-composer__footer-spacer" />
          ) : (
            <ChatExecutionPicker
              control={execution}
              disabled={pickerDisabled}
            />
          )}
          {primaryAction === undefined ? (
            <Button
              key="send"
              type="submit"
              variant="primary"
              className="wb-chat-composer__send"
              disabled={!canSend}
            >
              {isSending ? <Spinner label="Sending message" /> : "Send"}
            </Button>
          ) : (
            <HelpTarget
              key="host-action"
              content={primaryAction.help}
              reactAriaComposite
            >
              <Button
                ref={primaryAction.buttonRef}
                type="button"
                variant="primary"
                className="wb-chat-composer__send"
                disabled={!canRunPrimaryAction}
                onClick={() => {
                  void runPrimaryAction();
                }}
              >
                {isActionPending ? (
                  <Spinner
                    label={primaryAction.pendingLabel ?? `${primaryAction.label}…`}
                  />
                ) : primaryAction.label}
              </Button>
            </HelpTarget>
          )}
        </div>
      </div>
    </form>
  );
}
