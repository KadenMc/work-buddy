import { ArrowSquareOut } from "@phosphor-icons/react/ArrowSquareOut";
import { ArrowsOutLineHorizontal } from "@phosphor-icons/react/ArrowsOutLineHorizontal";
import { Clock } from "@phosphor-icons/react/Clock";
import { PencilSimple } from "@phosphor-icons/react/PencilSimple";
import { Trash } from "@phosphor-icons/react/Trash";
import { X } from "@phosphor-icons/react/X";
import type { ReactNode, RefObject } from "react";
import { Dialog, Heading, Popover } from "react-aria-components";

import { Button, IconButton } from "../../../ui";
import type {
  CalendarItemActionDescriptor,
  CalendarItemActionResolution,
} from "./actions";
import type { CalendarSurfaceItem, CalendarSurfaceSource } from "./contracts";
import { calendarItemTimeLabel } from "./format";
import "./calendar-item-inspector.css";

const actionIcon = (action: CalendarItemActionDescriptor): ReactNode => {
  if (action.icon === "edit-time") return <PencilSimple weight="duotone" />;
  if (action.icon === "duration") return <ArrowsOutLineHorizontal weight="duotone" />;
  if (action.icon === "remove") return <Trash weight="duotone" />;
  if (action.icon === "source") return <ArrowSquareOut weight="duotone" />;
  return action.icon === "open" ? <ArrowSquareOut weight="bold" /> : <Clock />;
};

export function CalendarItemInspector({
  item,
  source,
  timezone,
  resolution,
  triggerRef,
  onAction,
  onClose,
}: {
  readonly item: CalendarSurfaceItem;
  readonly source?: CalendarSurfaceSource;
  readonly timezone: string;
  readonly resolution: CalendarItemActionResolution;
  readonly triggerRef: RefObject<Element | null>;
  onAction(action: CalendarItemActionDescriptor): void;
  onClose(): void;
}) {
  const groups = (["primary", "edit", "danger"] as const)
    .map((group) => ({
      group,
      actions: resolution.actions.filter((action) => action.group === group),
    }))
    .filter(({ actions }) => actions.length > 0);

  return (
    <Popover
      isOpen
      triggerRef={triggerRef}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      placement="bottom start"
      shouldFlip
      className="wb-popover wb-calendar-inspector"
    >
      <Dialog className="wb-calendar-inspector__dialog">
        <header className="wb-calendar-inspector__header">
          <div>
            <span className="wb-calendar-inspector__kind">
              {item.kindLabel ?? item.kind}
            </span>
            <Heading slot="title">{item.title}</Heading>
          </div>
          <IconButton
            label="Close calendar item details"
            icon={<X weight="bold" />}
            variant="ghost"
            size="small"
            onClick={onClose}
          />
        </header>
        <dl className="wb-calendar-inspector__facts">
          <div><dt>When</dt><dd>{calendarItemTimeLabel(item, timezone)}</dd></div>
          <div><dt>Status</dt><dd>{item.status}</dd></div>
          {item.policy ? <div><dt>Policy</dt><dd>{item.policy.label}</dd></div> : null}
          <div><dt>Source</dt><dd>{source?.label ?? item.provenance.label}</dd></div>
        </dl>
        {item.policy?.description ? (
          <p className="wb-calendar-inspector__detail">{item.policy.description}</p>
        ) : null}
        {item.detail ? <p className="wb-calendar-inspector__detail">{item.detail}</p> : null}
        <div className="wb-calendar-inspector__groups">
          {groups.map(({ group, actions }) => (
            <section key={group} aria-label={group === "edit" ? "Edit actions" : undefined}>
              {actions.map((action) => (
                <Button
                  key={action.id}
                  size="small"
                  variant={action.tone === "danger" ? "danger" : group === "primary" ? "secondary" : "ghost"}
                  disabled={Boolean(action.disabledReason)}
                  title={action.disabledReason}
                  onClick={() => onAction(action)}
                >
                  <span aria-hidden="true">{actionIcon(action)}</span>
                  {action.label}
                </Button>
              ))}
            </section>
          ))}
        </div>
        {resolution.note ? <p className="wb-calendar-inspector__note">{resolution.note}</p> : null}
      </Dialog>
    </Popover>
  );
}
