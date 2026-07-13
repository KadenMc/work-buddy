import { type ReactNode, useId } from "react";

export interface WidgetFrameProps {
  readonly title: string;
  readonly children: ReactNode;
  readonly menu?: ReactNode;
  readonly status?: ReactNode;
  readonly busy?: boolean;
  readonly className?: string;
}

export function WidgetFrame({
  title,
  children,
  menu,
  status,
  busy = false,
  className = "",
}: WidgetFrameProps) {
  const titleId = useId();
  return (
    <section
      className={`wb-surface wb-widget-frame ${className}`.trim()}
      aria-labelledby={titleId}
      aria-busy={busy || undefined}
    >
      <header className="wb-widget-frame__header">
        <h2 id={titleId} className="wb-widget-frame__title">
          {title}
        </h2>
        {menu}
      </header>
      <div className="wb-widget-frame__content">
        {status}
        {children}
      </div>
    </section>
  );
}
