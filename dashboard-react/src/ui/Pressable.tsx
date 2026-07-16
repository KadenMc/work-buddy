import { forwardRef, type ReactNode } from "react";
import {
  Button as AriaButton,
  type ButtonProps as AriaButtonProps,
} from "react-aria-components";

export interface PressableProps
  extends Omit<AriaButtonProps, "children" | "className" | "onPress"> {
  readonly children: ReactNode;
  readonly className?: string;
  onClick(): void;
}

/** Unopinionated accessible press behavior for domain-specific surfaces. */
export const Pressable = forwardRef<HTMLButtonElement, PressableProps>(
  function Pressable({ children, className = "", onClick, ...props }, ref) {
    return (
      <AriaButton
        {...props}
        ref={ref}
        onPress={onClick}
        className={`wb-pressable ${className}`.trim()}
      >
        {children}
      </AriaButton>
    );
  },
);
