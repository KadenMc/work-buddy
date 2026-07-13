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
}

export function WidgetMenu({
  widgetTitle,
  presence,
  lockedReason,
  onRetry,
  onConfigure,
  onHide,
  onRemove,
}: WidgetMenuProps) {
  if (!onRetry && !onConfigure && !onHide && !onRemove) {
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
