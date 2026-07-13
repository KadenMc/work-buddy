import type { HTMLAttributes } from "react";

export function VisuallyHidden({
  className = "",
  ...props
}: HTMLAttributes<HTMLSpanElement>) {
  return (
    <span {...props} className={`wb-visually-hidden ${className}`.trim()} />
  );
}
