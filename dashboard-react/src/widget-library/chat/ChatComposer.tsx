import {
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { TextArea, TextField } from "react-aria-components";

import { Button, InlineAlert, Spinner } from "../../ui";
import { ChatExecutionPicker } from "./ChatExecutionPicker";
import type { ChatExecutionControl } from "./useChatExecutionProfile";
import "./styles.css";

/** Cap the auto-grown textarea height so a long draft scrolls within itself. */
const MAX_INPUT_HEIGHT = 160;

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
  /** Additive host context controls rendered beside the shared composer. */
  readonly accessory?: ReactNode;
}

export function ChatComposer({
  onSend,
  disabled = false,
  sending = false,
  placeholder = "Type a message…",
  label = "Message",
  errorMessage,
  initialValue = "",
  onDraftChange,
  execution,
  accessory,
}: ChatComposerProps) {
  const [draft, setDraft] = useState(initialValue);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const isSending = sending || busy;
  const effectiveDisabled =
    disabled || execution?.snapshot?.readOnly === true;
  const executionBlocksSend =
    execution?.selecting === true;
  const canSend =
    !effectiveDisabled &&
    !isSending &&
    !executionBlocksSend &&
    draft.trim().length > 0;

  const grow = (element: HTMLTextAreaElement | null) => {
    if (element === null) return;
    element.style.height = "auto";
    const next = Math.min(element.scrollHeight, MAX_INPUT_HEIGHT);
    if (next > 0) element.style.height = `${next}px`;
  };

  const submit = async () => {
    const value = draft.trim();
    if (
      value.length === 0 ||
      effectiveDisabled ||
      isSending ||
      executionBlocksSend
    ) {
      return;
    }
    setBusy(true);
    try {
      await onSend(value);
      setDraft("");
      onDraftChange?.("");
      if (inputRef.current !== null) {
        inputRef.current.style.height = "";
        inputRef.current.focus();
      }
    } catch {
      // Retain the draft. The panel surfaces the failure through errorMessage.
    } finally {
      setBusy(false);
    }
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
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
            grow(inputRef.current);
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
          {execution === undefined ? (
            <span className="wb-chat-composer__footer-spacer" />
          ) : (
            <ChatExecutionPicker
              control={execution}
              disabled={effectiveDisabled || isSending}
            />
          )}
          <Button
            type="submit"
            variant="primary"
            className="wb-chat-composer__send"
            disabled={!canSend}
          >
            {isSending ? <Spinner label="Sending message" /> : "Send"}
          </Button>
        </div>
      </div>
    </form>
  );
}
