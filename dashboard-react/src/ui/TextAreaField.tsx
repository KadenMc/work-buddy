import { forwardRef, type ReactNode } from "react";
import {
  Label,
  Text,
  TextArea,
  TextField,
  type TextAreaProps,
} from "react-aria-components";

export interface TextAreaFieldProps
  extends Omit<TextAreaProps, "className" | "value" | "onChange" | "disabled"> {
  readonly label: string;
  readonly value: string;
  readonly description?: ReactNode;
  readonly className?: string;
  readonly disabled?: boolean;
  onChange(value: string): void;
}

export const TextAreaField = forwardRef<HTMLTextAreaElement, TextAreaFieldProps>(
  function TextAreaField(
    {
      label,
      value,
      description,
      className = "",
      disabled,
      onChange,
      ...props
    },
    ref,
  ) {
    return (
      <TextField
        className={`wb-field ${className}`.trim()}
        value={value}
        isDisabled={disabled}
        onChange={onChange}
      >
        <Label className="wb-field__label">{label}</Label>
        <TextArea {...props} ref={ref} className="wb-textarea" />
        {description ? (
          <Text slot="description" className="wb-field__description">
            {description}
          </Text>
        ) : null}
      </TextField>
    );
  },
);
