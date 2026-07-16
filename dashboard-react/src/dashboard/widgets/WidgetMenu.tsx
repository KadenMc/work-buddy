import { EyeSlash } from "@phosphor-icons/react/EyeSlash";
import { Trash } from "@phosphor-icons/react/Trash";

import { ActionMenu, type ActionMenuSectionDefinition } from "../../ui";
import type { DefaultWidgetSlot } from "../contributions/contracts";

export interface WidgetMenuProps {
  readonly widgetTitle: string;
  readonly presence?: DefaultWidgetSlot["presence"];
  readonly lockedReason?: string;
  readonly onHide?: () => void;
  readonly onRemove?: () => void;
}

export function WidgetMenu({
  widgetTitle,
  presence,
  lockedReason,
  onHide,
  onRemove,
}: WidgetMenuProps) {
  if (!onHide && !onRemove) return null;
  const required = presence === "required";
  const sections: ActionMenuSectionDefinition[] = [];
  const visibility = [
    ...(onHide
      ? [{ id: "hide", label: "Hide", icon: <EyeSlash />, disabled: required, onAction: onHide }]
      : []),
    ...(onRemove
      ? [{ id: "remove", label: "Remove", icon: <Trash />, disabled: required, tone: "danger" as const, onAction: onRemove }]
      : []),
  ];
  if (visibility.length > 0) sections.push({ id: "visibility", items: visibility });

  return (
    <ActionMenu
      label={`Actions for ${widgetTitle}`}
      sections={sections}
      note={
        required && (onHide || onRemove)
          ? lockedReason ?? "This widget is required for the view's primary purpose."
          : undefined
      }
    />
  );
}
