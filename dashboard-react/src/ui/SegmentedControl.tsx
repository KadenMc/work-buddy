import { Label, Radio, RadioGroup, Text } from "react-aria-components";

export interface SegmentedControlOption<Value extends string> {
  readonly value: Value;
  readonly label: string;
  readonly disabled?: boolean;
}

export function SegmentedControl<Value extends string>({
  label,
  value,
  options,
  description,
  disabled,
  onChange,
}: {
  readonly label: string;
  readonly value: Value;
  readonly options: readonly SegmentedControlOption<Value>[];
  readonly description?: string;
  readonly disabled?: boolean;
  onChange(value: Value): void;
}) {
  return (
    <RadioGroup
      className="wb-segmented-field"
      value={value}
      isDisabled={disabled}
      onChange={(next: string) => onChange(next as Value)}
    >
      <Label className="wb-field__label">{label}</Label>
      <div className="wb-segmented-control">
        {options.map((option) => (
          <Radio
            key={option.value}
            value={option.value}
            isDisabled={option.disabled}
            className="wb-segmented-control__item"
          >
            {option.label}
          </Radio>
        ))}
      </div>
      {description ? (
        <Text slot="description" className="wb-field__description">
          {description}
        </Text>
      ) : null}
    </RadioGroup>
  );
}
