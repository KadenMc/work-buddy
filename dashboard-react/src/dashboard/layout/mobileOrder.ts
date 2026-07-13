import type { WidgetInstanceId, WidgetSlotId } from "../contributions/contracts";
import type { WidgetLayoutItem } from "./contracts";

export interface MobileOrderItem {
  readonly instanceId: WidgetInstanceId;
  readonly slotId?: WidgetSlotId;
  readonly visibility: "shown" | "hidden";
  readonly layout: WidgetLayoutItem;
}

const byDesktopReadingPosition = (left: MobileOrderItem, right: MobileOrderItem): number =>
  left.layout.y - right.layout.y ||
  left.layout.x - right.layout.x ||
  left.instanceId.localeCompare(right.instanceId);

/**
 * Produces one canonical mobile sequence: valid personal overrides first, App reading
 * order next, then deterministic desktop-position order for additions/missing entries.
 */
export function deriveMobileOrder(
  items: readonly MobileOrderItem[],
  readingOrder: readonly WidgetSlotId[],
  override?: readonly WidgetInstanceId[],
): readonly WidgetInstanceId[] {
  const visible = items.filter((item) => item.visibility === "shown");
  const byInstance = new Map(visible.map((item) => [item.instanceId, item]));
  const bySlot = new Map(
    visible
      .filter((item): item is MobileOrderItem & { readonly slotId: WidgetSlotId } =>
        item.slotId !== undefined,
      )
      .map((item) => [item.slotId, item]),
  );
  const result: WidgetInstanceId[] = [];
  const append = (instanceId: WidgetInstanceId): void => {
    if (byInstance.has(instanceId) && !result.includes(instanceId)) result.push(instanceId);
  };

  override?.forEach(append);
  readingOrder.forEach((slotId) => {
    const item = bySlot.get(slotId);
    if (item !== undefined) append(item.instanceId);
  });
  [...visible].sort(byDesktopReadingPosition).forEach((item) => append(item.instanceId));
  return result;
}

export function moveMobileOrderItem(
  order: readonly WidgetInstanceId[],
  instanceId: WidgetInstanceId,
  direction: "before" | "after",
): readonly WidgetInstanceId[] {
  const index = order.indexOf(instanceId);
  if (index < 0) return order;
  const target = direction === "before" ? index - 1 : index + 1;
  if (target < 0 || target >= order.length) return order;
  const next = [...order];
  [next[index], next[target]] = [next[target]!, next[index]!];
  return next;
}

export function orderItemsForMobile<Item extends MobileOrderItem>(
  items: readonly Item[],
  order: readonly WidgetInstanceId[],
): readonly Item[] {
  const byId = new Map(items.map((item) => [item.instanceId, item]));
  return order.flatMap((instanceId) => {
    const item = byId.get(instanceId);
    return item === undefined || item.visibility === "hidden" ? [] : [item];
  });
}

