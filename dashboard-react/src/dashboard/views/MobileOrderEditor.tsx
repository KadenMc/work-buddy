import { DotsSixVertical } from "@phosphor-icons/react/DotsSixVertical";
import { X } from "@phosphor-icons/react/X";
import {
  Button as AriaButton,
  GridList,
  GridListItem,
  useDragAndDrop,
} from "react-aria-components";
import type { Key } from "react";

import { Button } from "../../ui";
import type { ContributionRegistry } from "../contributions/registry";
import type { WidgetInstanceId } from "../contributions/contracts";
import type { EffectiveWidgetInstance } from "../personalization/contracts";

export interface MobileOrderEditorProps {
  readonly registry: ContributionRegistry;
  readonly instances: readonly EffectiveWidgetInstance[];
  readonly order: readonly WidgetInstanceId[];
  onChange(order: readonly WidgetInstanceId[]): void;
  onClose(): void;
}

interface MobileOrderDropTarget {
  readonly key: Key;
  readonly dropPosition: "before" | "after" | "on";
}

export const reorderMobileWidgets = (
  order: readonly WidgetInstanceId[],
  movingKeys: Iterable<Key>,
  target: MobileOrderDropTarget,
): readonly WidgetInstanceId[] => {
  const movingIds = new Set([...movingKeys].map(String));
  const moving = order.filter((instanceId) => movingIds.has(instanceId));
  if (moving.length === 0) return order;

  const remaining = order.filter((instanceId) => !movingIds.has(instanceId));
  const targetIndex = remaining.findIndex((instanceId) => instanceId === String(target.key));
  if (targetIndex < 0) return order;
  const insertAt = targetIndex + (target.dropPosition === "after" ? 1 : 0);
  remaining.splice(insertAt, 0, ...moving);
  return remaining;
};

export function MobileOrderEditor({
  registry,
  instances,
  order,
  onChange,
  onClose,
}: MobileOrderEditorProps) {
  const visibleInstances = instances.filter((instance) => instance.visibility === "shown");
  const byId = new Map(visibleInstances.map((instance) => [instance.instanceId, instance]));
  const normalized = [
    ...order.filter((instanceId) => byId.has(instanceId)),
    ...visibleInstances
      .map((instance) => instance.instanceId)
      .filter((instanceId) => !order.includes(instanceId)),
  ];
  const items = normalized.map((instanceId) => {
    const instance = byId.get(instanceId)!;
    const widget = registry.getWidget(instance.widgetTypeId);
    return {
      id: instanceId,
      title: widget?.definition.displayName ?? instance.widgetTypeId,
    };
  });
  const { dragAndDropHooks } = useDragAndDrop({
    getItems: (keys) =>
      [...keys].map((key) => ({
        "text/plain": String(key),
      })),
    getAllowedDropOperations: () => ["move"],
    onReorder: (event) => {
      onChange(reorderMobileWidgets(normalized, event.keys, event.target));
    },
  });

  return (
    <section className="wb-mobile-order-editor" aria-labelledby="wb-mobile-order-title">
      <header>
        <div>
          <h2 id="wb-mobile-order-title">Mobile order</h2>
          <p>Set the one-column reading, focus, and screen-reader order used on mobile.</p>
        </div>
        <Button size="small" variant="ghost" onClick={onClose}>
          <X aria-hidden="true" /> Close
        </Button>
      </header>
      <GridList
        aria-label="Mobile widget order"
        items={items}
        selectionMode="none"
        dragAndDropHooks={dragAndDropHooks}
        className="wb-mobile-order-editor__list"
      >
        {(item) => (
          <GridListItem id={item.id} textValue={item.title} className="wb-mobile-order-editor__item">
            <AriaButton
              slot="drag"
              className="wb-mobile-order-editor__drag-handle"
              aria-label={`Drag ${item.title} to reorder on mobile`}
            >
              <DotsSixVertical weight="bold" aria-hidden="true" />
            </AriaButton>
            <strong>{item.title}</strong>
            <span className="wb-mobile-order-editor__hint">Drag to reorder</span>
          </GridListItem>
        )}
      </GridList>
    </section>
  );
}
