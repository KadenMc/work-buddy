import { forwardRef, type ReactNode } from "react";
import {
  Button as AriaButton,
  type ButtonProps as AriaButtonProps,
} from "react-aria-components";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "small" | "medium" | "large";

export interface ButtonProps
  extends Omit<
    AriaButtonProps,
    "children" | "className" | "onPress" | "isDisabled"
  > {
  readonly children?: ReactNode;
  readonly className?: string;
  readonly variant?: ButtonVariant;
  readonly size?: ButtonSize;
  readonly disabled?: boolean;
  readonly title?: string;
  readonly onClick?: () => void;
}

/** Work Buddy's stable button API. React Aria remains a private behavior layer. */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    children,
    className = "",
    variant = "secondary",
    size = "medium",
    disabled,
    onClick,
    ...props
  },
  ref,
) {
  return (
    <AriaButton
      {...props}
      ref={ref}
      isDisabled={disabled}
      onPress={onClick}
      className={`wb-button wb-button--${variant} wb-button--${size} ${className}`.trim()}
    >
      {children}
    </AriaButton>
  );
});
