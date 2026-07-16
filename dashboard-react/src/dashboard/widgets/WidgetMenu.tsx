import { VisuallyHidden } from "../../ui";
import type { DefaultWidgetSlot } from "../contributions/contracts";

export interface WidgetMenuProps {
  readonly widgetTitle: string;
  readonly presence?: DefaultWidgetSlot["presence"];
  readonly lockedReason?: string;
  readonly onRetry?: () => void;
  readonly onConfigure?: () => void;
  readonly onHide?: () => void;
  readonly onRemove?: () => void;
  readonly onMove?: (direction: "left" | "right" | "up" | "down") => void;
  readonly onResize?: (
    direction: "grow-width" | "shrink-width" | "grow-height" | "shrink-height",
  ) => void;
}

export function WidgetMenu({
  widgetTitle,
  presence,
  lockedReason,
  onRetry,
  onConfigure,
  onHide,
  onRemove,
  onMove,
  onResize,
}: WidgetMenuProps) {
  if (!onRetry && !onConfigure && !onHide && !onRemove && !onMove && !onResize) {
    return null;
  }
  const required = presence === "required";
  const explanation =
    lockedReason ?? "This widget is required for the view's primary purpose.";

  return (
    <details className="wb-widget-menu">
      <summary>
        <span aria-hidden="true">•••</span>
        <VisuallyHidden>{`Actions for ${widgetTitle}`}</VisuallyHidden>
      </summary>
      <div className="wb-widget-menu__popover" aria-label={`${widgetTitle} actions`}>
        {onRetry && (
          <button className="wb-widget-menu__item" type="button" onClick={onRetry}>
            Retry
          </button>
        )}
        {onConfigure && (
          <button
            className="wb-widget-menu__item"
            type="button"
            onClick={onConfigure}
          >
            Configure
          </button>
        )}
        {onMove && (
          <fieldset className="wb-widget-menu__group">
            <legend>Move</legend>
            {(["left", "right", "up", "down"] as const).map((direction) => (
              <button
                key={direction}
                className="wb-widget-menu__item"
                type="button"
                onClick={() => onMove(direction)}
              >
                {direction}
              </button>
            ))}
          </fieldset>
        )}
        {onResize && (
          <fieldset className="wb-widget-menu__group">
            <legend>Resize</legend>
            {(
              [
                ["grow-width", "Wider"],
                ["shrink-width", "Narrower"],
                ["grow-height", "Taller"],
                ["shrink-height", "Shorter"],
              ] as const
            ).map(([direction, label]) => (
              <button
                key={direction}
                className="wb-widget-menu__item"
                type="button"
                onClick={() => onResize(direction)}
              >
                {label}
              </button>
            ))}
          </fieldset>
        )}
        {onHide && (
          <button
            className="wb-widget-menu__item"
            type="button"
            disabled={required}
            onClick={required ? undefined : onHide}
          >
            Hide
          </button>
        )}
        {onRemove && (
          <button
            className="wb-widget-menu__item"
            type="button"
            disabled={required}
            onClick={required ? undefined : onRemove}
          >
            Remove
          </button>
        )}
        {required && (onHide || onRemove) && (
          <p className="wb-widget-menu__note">{explanation}</p>
        )}
      </div>
    </details>
  );
}
