import { ArrowClockwise } from "@phosphor-icons/react/ArrowClockwise";
import { ArrowDown } from "@phosphor-icons/react/ArrowDown";
import { ArrowLeft } from "@phosphor-icons/react/ArrowLeft";
import { ArrowRight } from "@phosphor-icons/react/ArrowRight";
import { ArrowUp } from "@phosphor-icons/react/ArrowUp";
import { ArrowsInLineHorizontal } from "@phosphor-icons/react/ArrowsInLineHorizontal";
import { ArrowsInLineVertical } from "@phosphor-icons/react/ArrowsInLineVertical";
import { ArrowsOutLineHorizontal } from "@phosphor-icons/react/ArrowsOutLineHorizontal";
import { ArrowsOutLineVertical } from "@phosphor-icons/react/ArrowsOutLineVertical";
import { EyeSlash } from "@phosphor-icons/react/EyeSlash";
import { GearSix } from "@phosphor-icons/react/GearSix";
import { Trash } from "@phosphor-icons/react/Trash";

import { ActionMenu, type ActionMenuSectionDefinition } from "../../ui";
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
  const sections: ActionMenuSectionDefinition[] = [];
  const general = [
    ...(onRetry
      ? [{ id: "retry", label: "Retry", icon: <ArrowClockwise />, onAction: onRetry }]
      : []),
    ...(onConfigure
      ? [{ id: "configure", label: "Configure", icon: <GearSix />, onAction: onConfigure }]
      : []),
  ];
  if (general.length > 0) sections.push({ id: "general", items: general });

  if (onMove) {
    sections.push({
      id: "move",
      label: "Move",
      items: [
        { id: "move-left", label: "left", icon: <ArrowLeft />, onAction: () => onMove("left") },
        { id: "move-right", label: "right", icon: <ArrowRight />, onAction: () => onMove("right") },
        { id: "move-up", label: "up", icon: <ArrowUp />, onAction: () => onMove("up") },
        { id: "move-down", label: "down", icon: <ArrowDown />, onAction: () => onMove("down") },
      ],
    });
  }

  if (onResize) {
    sections.push({
      id: "resize",
      label: "Resize",
      items: [
        { id: "grow-width", label: "Wider", icon: <ArrowsOutLineHorizontal />, onAction: () => onResize("grow-width") },
        { id: "shrink-width", label: "Narrower", icon: <ArrowsInLineHorizontal />, onAction: () => onResize("shrink-width") },
        { id: "grow-height", label: "Taller", icon: <ArrowsOutLineVertical />, onAction: () => onResize("grow-height") },
        { id: "shrink-height", label: "Shorter", icon: <ArrowsInLineVertical />, onAction: () => onResize("shrink-height") },
      ],
    });
  }

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
