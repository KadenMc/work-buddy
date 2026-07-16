import { forwardRef, type ReactNode, useId } from "react";

import type { WidgetPresentationContext } from "../contributions/contracts";
import { HelpTarget, type HelpContent } from "../help";

export interface WidgetFrameProps {
  readonly title: string;
  readonly icon?: ReactNode;
  readonly children: ReactNode;
  readonly headerMeta?: ReactNode;
  readonly menu?: ReactNode;
  readonly help?: HelpContent;
  readonly status?: ReactNode;
  readonly busy?: boolean;
  readonly className?: string;
  readonly interactionMode?: WidgetPresentationContext["interactionMode"];
}

export const WidgetFrame = forwardRef<HTMLElement, WidgetFrameProps>(function WidgetFrame({
  title,
  icon,
  children,
  headerMeta,
  menu,
  help,
  status,
  busy = false,
  className = "",
  interactionMode = "operate",
}: WidgetFrameProps, ref) {
  const titleId = useId();
  return (
    <section
      ref={ref}
      className={`wb-surface wb-widget-frame ${className}`.trim()}
      aria-labelledby={titleId}
      aria-busy={busy || undefined}
      data-widget-interaction-mode={interactionMode}
    >
      <header className="wb-widget-frame__header">
        <HelpTarget
          content={help}
          placement="bottom start"
          focusable
          ariaLabel={`About ${title} in this view`}
        >
          <div className="wb-widget-frame__identity">
            {icon ? (
              <span className="wb-widget-frame__icon" aria-hidden="true">
                {icon}
              </span>
            ) : null}
            <h2 id={titleId} className="wb-widget-frame__title">
              {title}
            </h2>
          </div>
        </HelpTarget>
        {headerMeta || menu ? (
          <div className="wb-widget-frame__header-tools">
            {headerMeta}
            {menu}
          </div>
        ) : null}
      </header>
      <div
        className="wb-widget-frame__content"
        data-scroll-boundary-policy="native"
        inert={interactionMode === "arrange"}
      >
        {status}
        {children}
      </div>
      {interactionMode === "arrange" ? (
        <div className="wb-widget-frame__interaction-shield">
          <span>Interactions paused while arranging</span>
        </div>
      ) : null}
    </section>
  );
});
