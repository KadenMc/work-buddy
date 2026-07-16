import type { HTMLAttributes } from "react";

export type InlineAlertTone = "info" | "warning" | "danger" | "success";

export interface InlineAlertProps extends HTMLAttributes<HTMLDivElement> {
  readonly tone?: InlineAlertTone;
}

export function InlineAlert({
  className = "",
  tone = "info",
  ...props
}: InlineAlertProps) {
  return (
    <div
      {...props}
      className={`wb-inline-alert wb-inline-alert--${tone} ${className}`.trim()}
    />
  );
}
