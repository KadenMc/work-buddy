import type { ElementType, HTMLAttributes, ReactNode } from "react";

export function Text({
  as: Component = "span",
  tone = "default",
  size = "medium",
  className = "",
  children,
  ...props
}: HTMLAttributes<HTMLElement> & {
  readonly as?: ElementType;
  readonly tone?: "strong" | "default" | "secondary" | "muted";
  readonly size?: "small" | "medium" | "large";
  readonly children?: ReactNode;
}) {
  return (
    <Component
      {...props}
      className={`wb-text wb-text--${tone} wb-text--${size} ${className}`.trim()}
    >
      {children}
    </Component>
  );
}

export function Stack({ className = "", ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div {...props} className={`wb-stack ${className}`.trim()} />;
}

export function Inline({ className = "", ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div {...props} className={`wb-inline ${className}`.trim()} />;
}
