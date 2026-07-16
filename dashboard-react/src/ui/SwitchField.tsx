import { Switch } from "react-aria-components";

import { HelpTarget, type HelpContent } from "../dashboard/help";

export interface SwitchFieldProps {
  readonly label: string;
  readonly help?: HelpContent;
  readonly selected: boolean;
  readonly disabled?: boolean;
  readonly className?: string;
  onChange(selected: boolean): void;
}

export function SwitchField({
  label,
  help,
  selected,
  disabled,
  className = "",
  onChange,
}: SwitchFieldProps) {
  const control = (
    <Switch
      className={`wb-switch-field ${className}`.trim()}
      aria-label={label}
      isSelected={selected}
      isDisabled={disabled}
      onChange={onChange}
    >
      <span className="wb-switch-field__copy">
        <span className="wb-switch-field__label">{label}</span>
      </span>
      <span className="wb-switch-field__track" aria-hidden="true">
        <span className="wb-switch-field__thumb" />
      </span>
    </Switch>
  );
  return <HelpTarget content={help} reactAriaComposite>{control}</HelpTarget>;
}
