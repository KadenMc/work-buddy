import { CaretDown } from "@phosphor-icons/react/CaretDown";
import { Check } from "@phosphor-icons/react/Check";
import type { ReactNode } from "react";
import {
  Button as AriaButton,
  Label,
  ListBox,
  ListBoxItem,
  Popover,
  Select,
  SelectValue,
  Text,
  type Key,
} from "react-aria-components";

import { HelpTarget, type HelpContent } from "../dashboard/help";

export interface SelectFieldOption<Value extends string> {
  readonly value: Value;
  readonly label: string;
  readonly description?: string;
  readonly disabled?: boolean;
}

export interface SelectFieldProps<Value extends string> {
  readonly label: string;
  readonly value: Value;
  readonly options: readonly SelectFieldOption<Value>[];
  readonly description?: ReactNode;
  readonly help?: HelpContent;
  readonly className?: string;
  readonly disabled?: boolean;
  readonly compact?: boolean;
  /** Keep the accessible label while removing it from the visual layout. */
  readonly hideLabel?: boolean;
  onChange(value: Value): void;
}

export function SelectField<Value extends string>({
  label,
  value,
  options,
  description,
  help,
  className = "",
  disabled,
  compact = false,
  hideLabel = false,
  onChange,
}: SelectFieldProps<Value>) {
  return (
    <Select
      className={`wb-field wb-select-field${compact ? " wb-select-field--compact" : ""}${hideLabel || compact ? " wb-select-field--label-hidden" : ""} ${className}`.trim()}
      selectedKey={value}
      isDisabled={disabled}
      onSelectionChange={(key: Key | null) => {
        if (key !== null) onChange(String(key) as Value);
      }}
    >
      <Label className="wb-field__label">{label}</Label>
      <HelpTarget content={help} placement="bottom start" reactAriaComposite>
        <AriaButton className="wb-select-field__trigger">
          <SelectValue>{({ selectedText }) => selectedText}</SelectValue>
          <CaretDown weight="bold" aria-hidden="true" />
        </AriaButton>
      </HelpTarget>
      {description ? (
        <Text slot="description" className="wb-field__description">
          {description}
        </Text>
      ) : null}
      <Popover className="wb-popover wb-select-field__popover" placement="bottom start">
        <ListBox className="wb-listbox" items={options}>
          {(option) => (
            <ListBoxItem
              id={option.value}
              textValue={option.label}
              isDisabled={option.disabled}
              className="wb-listbox__item"
            >
              {({ isSelected }) => (
                <>
                  <span className="wb-listbox__check" aria-hidden="true">
                    {isSelected ? <Check weight="bold" /> : null}
                  </span>
                  <span className="wb-listbox__copy">
                    <span>{option.label}</span>
                    {option.description ? <small>{option.description}</small> : null}
                  </span>
                </>
              )}
            </ListBoxItem>
          )}
        </ListBox>
      </Popover>
    </Select>
  );
}
