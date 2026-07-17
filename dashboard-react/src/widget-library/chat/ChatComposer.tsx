import {
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { TextArea, TextField } from "react-aria-components";

import { Button, InlineAlert, Spinner } from "../../ui";
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
}

export function ChatComposer({
  onSend,
  disabled = false,
  sending = false,
  placeholder = "Type a message…",
  label = "Message",
  errorMessage,
}: ChatComposerProps) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const isSending = sending || busy;
  const canSend = !disabled && !isSending && draft.trim().length > 0;

  const grow = (element: HTMLTextAreaElement | null) => {
    if (element === null) return;
    element.style.height = "auto";
    const next = Math.min(element.scrollHeight, MAX_INPUT_HEIGHT);
    if (next > 0) element.style.height = `${next}px`;
  };

  const submit = async () => {
    const value = draft.trim();
    if (value.length === 0 || disabled || isSending) return;
    setBusy(true);
    try {
      await onSend(value);
      setDraft("");
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
      <div className="wb-chat-composer__row">
        <TextField
          className="wb-chat-composer__field"
          aria-label={label}
          value={draft}
          isDisabled={disabled}
          onChange={(value) => {
            setDraft(value);
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
        <Button
          type="submit"
          variant="primary"
          className="wb-chat-composer__send"
          disabled={!canSend}
        >
          {isSending ? <Spinner label="Sending message" /> : "Send"}
        </Button>
      </div>
    </form>
  );
}
