import type { HTMLAttributes } from "react";

export function Surface({
  className = "",
  ...props
}: HTMLAttributes<HTMLDivElement>) {
  return <div {...props} className={`wb-surface ${className}`.trim()} />;
}
