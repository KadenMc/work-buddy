import { forwardRef, type ReactNode } from "react";

import { Button, type ButtonProps } from "./Button";

export interface IconButtonProps extends Omit<ButtonProps, "children" | "aria-label"> {
  readonly label: string;
  readonly icon: ReactNode;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, className = "", ...props },
  ref,
) {
  return (
    <Button
      {...props}
      ref={ref}
      aria-label={label}
      title={props.title ?? label}
      className={`wb-icon-button ${className}`.trim()}
    >
      <span className="wb-icon-button__icon" aria-hidden="true">
        {icon}
      </span>
    </Button>
  );
});
