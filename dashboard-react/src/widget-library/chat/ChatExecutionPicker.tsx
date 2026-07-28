import { CaretDown } from "@phosphor-icons/react/CaretDown";
import { Check } from "@phosphor-icons/react/Check";
import { useId, useRef, useState } from "react";
import {
  Button as AriaButton,
  Dialog,
  Header,
  Heading,
  Label,
  ListBox,
  ListBoxItem,
  ListBoxSection,
  Modal,
  ModalOverlay,
  Popover,
  Select,
  type Key,
} from "react-aria-components";

import { Button, InlineAlert, Spinner } from "../../ui";
import type {
  ChatExecutionModelOption,
  ChatExecutionProviderOption,
} from "./contracts";
import type {
  ChatExecutionControl,
  ChatExecutionSwitchConfirmation,
} from "./useChatExecutionProfile";
import "./styles.css";

export interface ChatExecutionPickerProps {
  readonly control: ChatExecutionControl;
  /** Host-level lock, e.g. while one response is being generated. */
  readonly disabled?: boolean;
  /** Render truthful current metadata without an interactive listbox. */
  readonly readOnly?: boolean;
  readonly className?: string;
}

interface ExecutionChoice {
  readonly provider: ChatExecutionProviderOption;
  readonly model: ChatExecutionModelOption;
}

const choiceKey = (providerId: string, modelId: string): string =>
  JSON.stringify([providerId, modelId]);

const selectedLabel = (control: ChatExecutionControl): string => {
  const selection = control.snapshot?.selection;
  if (selection === undefined) {
    return control.status === "loading"
      ? "Checking models…"
      : "Models unavailable";
  }
  return `${selection.providerLabel} · ${selection.modelLabel}`;
};

function unavailableText(
  provider: ChatExecutionProviderOption,
  model: ChatExecutionModelOption,
): string | undefined {
  if (!provider.available) {
    return provider.unavailableReason ?? "Provider unavailable";
  }
  if (!model.available) {
    return model.unavailableReason ?? "Model unavailable";
  }
  return undefined;
}

function currentUnavailableText(
  control: ChatExecutionControl,
): string | undefined {
  const snapshot = control.snapshot;
  if (snapshot === null || control.currentAvailable) return undefined;
  const provider = snapshot.providers.find(
    (candidate) => candidate.id === snapshot.selection.providerId,
  );
  if (provider === undefined) {
    return "The selected provider is no longer available.";
  }
  if (!provider.available) {
    return provider.unavailableReason ?? "The selected provider is unavailable.";
  }
  const model = provider.models.find(
    (candidate) => candidate.id === snapshot.selection.modelId,
  );
  if (model === undefined) {
    return "The selected model is no longer available.";
  }
  return model.unavailableReason ?? "The selected model is unavailable.";
}

export function ChatExecutionPicker({
  control,
  disabled = false,
  readOnly = false,
  className = "",
}: ChatExecutionPickerProps) {
  const messageId = useId();
  const unavailableId = useId();
  const confirmationTitleId = useId();
  const confirmationDescriptionId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [pendingChoice, setPendingChoice] = useState<ExecutionChoice | null>(
    null,
  );
  const [confirmation, setConfirmation] =
    useState<ChatExecutionSwitchConfirmation | null>(null);
  const snapshot = control.snapshot;
  const display = selectedLabel(control);
  const currentUnavailable = currentUnavailableText(control);

  if (snapshot === null) {
    return (
      <div
        className={`wb-chat-execution wb-chat-execution--unavailable ${className}`.trim()}
        aria-label="Run chat with"
      >
        <span className="wb-chat-execution__label">Run with</span>
        <Button
          variant="secondary"
          size="small"
          disabled
          className="wb-chat-execution__fallback-trigger"
        >
          {control.status === "loading" ? (
            <Spinner label="Checking available models" />
          ) : (
            display
          )}
        </Button>
        {control.status === "error" ? (
          <InlineAlert
            tone="danger"
            role="alert"
            className="wb-chat-execution__message"
          >
            <span>{control.error ?? "Models could not be loaded."}</span>
            <Button
              variant="secondary"
              size="small"
              onClick={control.retry}
              disabled={disabled}
            >
              Try again
            </Button>
          </InlineAlert>
        ) : null}
      </div>
    );
  }

  if (readOnly || snapshot.readOnly === true) {
    return (
      <div
        className={`wb-chat-execution wb-chat-execution--metadata ${className}`.trim()}
        aria-label={`Run with ${display}`}
      >
        <span className="wb-chat-execution__label">Run with</span>
        <span className="wb-chat-execution__metadata-value">{display}</span>
        {currentUnavailable === undefined ? null : (
          <span className="wb-chat-execution__metadata-unavailable">
            Unavailable: {currentUnavailable}
          </span>
        )}
      </div>
    );
  }

  const choices = new Map<string, ExecutionChoice>();
  for (const provider of snapshot.providers) {
    for (const model of provider.models) {
      choices.set(choiceKey(provider.id, model.id), {
        provider,
        model,
      });
    }
  }
  const selectedKey = choiceKey(
    snapshot.selection.providerId,
    snapshot.selection.modelId,
  );
  const pickerDisabled = disabled || control.selecting;
  const returnFocus = (): void => {
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  };
  const closeConfirmation = (): void => {
    setPendingChoice(null);
    setConfirmation(null);
    returnFocus();
  };
  const selectChoice = (choice: ExecutionChoice): void => {
    const impact = control.confirmSelection?.({
      providerId: choice.provider.id,
      modelId: choice.model.id,
      providerLabel: choice.provider.label,
      modelLabel: choice.model.label,
    });
    if (impact !== undefined && impact !== null) {
      setPendingChoice(choice);
      setConfirmation(impact);
      return;
    }
    void control.select(choice.provider.id, choice.model.id).catch(() => {});
  };

  return (
    <div
      className={`wb-chat-execution ${className}`.trim()}
      data-selecting={control.selecting || undefined}
    >
      <Select
        className="wb-chat-execution__select"
        selectedKey={selectedKey}
        isDisabled={pickerDisabled}
        aria-describedby={[
          control.error === null ? null : messageId,
          currentUnavailable === undefined ? null : unavailableId,
        ]
          .filter((id): id is string => id !== null)
          .join(" ") || undefined}
        onSelectionChange={(key: Key | null) => {
          if (key === null) return;
          const choice = choices.get(String(key));
          if (choice === undefined) return;
          selectChoice(choice);
        }}
      >
        <Label className="wb-visually-hidden">{`Run with ${display}`}</Label>
        <AriaButton
          ref={triggerRef}
          className="wb-chat-execution__trigger"
        >
          <span className="wb-chat-execution__label" aria-hidden="true">
            Run with
          </span>
          <span className="wb-chat-execution__value">{display}</span>
          {control.selecting ? (
            <Spinner label="Changing model" />
          ) : (
            <CaretDown weight="bold" aria-hidden="true" />
          )}
        </AriaButton>
        <Popover
          className="wb-popover wb-chat-execution__popover"
          placement="top start"
        >
          <ListBox className="wb-listbox wb-chat-execution__listbox">
            {snapshot.providers.map((provider) => (
              <ListBoxSection
                key={provider.id}
                className="wb-chat-execution__section"
              >
                <Header className="wb-chat-execution__provider">
                  <span>{provider.label}</span>
                  {!provider.available ? (
                    <small>
                      {provider.unavailableReason ?? "Unavailable"}
                    </small>
                  ) : provider.description ? (
                    <small>{provider.description}</small>
                  ) : null}
                </Header>
                {provider.models.length === 0 ? (
                  <ListBoxItem
                    id={`provider-unavailable:${provider.id}`}
                    textValue={`${provider.label}, no models available`}
                    isDisabled
                    className="wb-listbox__item"
                  >
                    <span className="wb-listbox__check" aria-hidden="true" />
                    <span className="wb-listbox__copy">
                      <span>No models available</span>
                      <small>
                        {provider.unavailableReason ??
                          "This provider has no selectable models."}
                      </small>
                    </span>
                  </ListBoxItem>
                ) : (
                  provider.models.map((model) => {
                    const key = choiceKey(provider.id, model.id);
                    const unavailable = unavailableText(provider, model);
                    const accessibleDetail =
                      unavailable ?? model.description;
                    return (
                      <ListBoxItem
                        key={key}
                        id={key}
                        textValue={`${provider.label}, ${model.label}`}
                        aria-label={`${provider.label}, ${model.label}${accessibleDetail ? `, ${accessibleDetail}` : ""}`}
                        isDisabled={unavailable !== undefined}
                        className="wb-listbox__item"
                      >
                        {({ isSelected }) => (
                          <>
                            <span
                              className="wb-listbox__check"
                              aria-hidden="true"
                            >
                              {isSelected ? <Check weight="bold" /> : null}
                            </span>
                            <span className="wb-listbox__copy">
                              <span>{model.label}</span>
                              {unavailable !== undefined ? (
                                <small>{unavailable}</small>
                              ) : model.description ? (
                                <small>{model.description}</small>
                              ) : null}
                            </span>
                          </>
                        )}
                      </ListBoxItem>
                    );
                  })
                )}
              </ListBoxSection>
            ))}
          </ListBox>
        </Popover>
      </Select>
      {currentUnavailable === undefined ? null : (
        <div
          id={unavailableId}
          className="wb-chat-execution__availability"
          role="status"
        >
          <span>
            <strong>Unavailable:</strong> {currentUnavailable}
          </span>
          <Button
            variant="secondary"
            size="small"
            onClick={control.retry}
            disabled={disabled || control.selecting}
          >
            Check again
          </Button>
        </div>
      )}
      {control.error !== null ? (
        <InlineAlert
          id={messageId}
          tone="danger"
          role="alert"
          className="wb-chat-execution__message"
        >
          {control.error}
        </InlineAlert>
      ) : null}
      {control.announcement !== null ? (
        <span
          className="wb-visually-hidden"
          role="status"
          aria-live="polite"
        >
          {control.announcement}
        </span>
      ) : null}
      {pendingChoice !== null && confirmation !== null ? (
        <ModalOverlay
          className="wb-confirmation-overlay"
          isOpen
          isDismissable={!control.selecting}
          onOpenChange={(open) => {
            if (!open && !control.selecting) closeConfirmation();
          }}
        >
          <Modal className="wb-confirmation-modal">
            <Dialog
              className="wb-confirmation-dialog"
              aria-labelledby={confirmationTitleId}
              aria-describedby={confirmationDescriptionId}
            >
              <Heading
                id={confirmationTitleId}
                slot="title"
                className="wb-confirmation-dialog__title"
              >
                {confirmation.title}
              </Heading>
              <p
                id={confirmationDescriptionId}
                className="wb-confirmation-dialog__description"
              >
                {confirmation.description}
              </p>
              <div className="wb-confirmation-dialog__actions">
                <Button
                  onClick={closeConfirmation}
                  disabled={control.selecting}
                >
                  {confirmation.cancelLabel ?? "Cancel"}
                </Button>
                <Button
                  variant="primary"
                  disabled={disabled || control.selecting}
                  onClick={() => {
                    if (disabled || control.selecting) return;
                    const choice = pendingChoice;
                    setPendingChoice(null);
                    setConfirmation(null);
                    returnFocus();
                    void control
                      .select(choice.provider.id, choice.model.id)
                      .catch(() => {});
                  }}
                >
                  {confirmation.confirmLabel}
                </Button>
              </div>
            </Dialog>
          </Modal>
        </ModalOverlay>
      ) : null}
    </div>
  );
}

export default ChatExecutionPicker;
