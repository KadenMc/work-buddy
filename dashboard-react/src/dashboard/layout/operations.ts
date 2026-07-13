import { verticalCompactor, type Layout as RglLayout } from "react-grid-layout";

import type { GridSize, WidgetInstanceId } from "../contributions/contracts";
import {
  DASHBOARD_COLUMNS,
  type DashboardLayout,
  type LayoutCommand,
  type LayoutMutationResult,
  type WidgetLayoutItem,
} from "./contracts";

export const layoutsCollide = (
  left: WidgetLayoutItem,
  right: WidgetLayoutItem,
): boolean =>
  left.instanceId !== right.instanceId &&
  left.x < right.x + right.w &&
  left.x + left.w > right.x &&
  left.y < right.y + right.h &&
  left.y + left.h > right.y;

export const hasLayoutCollision = (
  item: WidgetLayoutItem,
  items: DashboardLayout,
): boolean => items.some((candidate) => layoutsCollide(item, candidate));

export const isLayoutItemWithinBounds = (
  item: WidgetLayoutItem,
  columns = DASHBOARD_COLUMNS,
): boolean =>
  Number.isInteger(item.x) &&
  Number.isInteger(item.y) &&
  Number.isInteger(item.w) &&
  Number.isInteger(item.h) &&
  item.x >= 0 &&
  item.y >= 0 &&
  item.w > 0 &&
  item.h > 0 &&
  item.x + item.w <= columns;

export const isLayoutItemWithinSizeLimits = (item: WidgetLayoutItem): boolean =>
  (item.minW === undefined || item.w >= item.minW) &&
  (item.maxW === undefined || item.w <= item.maxW) &&
  (item.minH === undefined || item.h >= item.minH) &&
  (item.maxH === undefined || item.h <= item.maxH);

export const validateDashboardLayout = (
  items: DashboardLayout,
  columns = DASHBOARD_COLUMNS,
): readonly string[] => {
  const issues: string[] = [];
  const ids = new Set<WidgetInstanceId>();
  items.forEach((item, index) => {
    if (ids.has(item.instanceId)) {
      issues.push(`items[${index}] duplicates instance ${item.instanceId}`);
    }
    ids.add(item.instanceId);
    if (!isLayoutItemWithinBounds(item, columns)) {
      issues.push(`items[${index}] is outside the ${columns}-column grid`);
    }
    if (!isLayoutItemWithinSizeLimits(item)) {
      issues.push(`items[${index}] violates its min/max size`);
    }
  });
  for (let left = 0; left < items.length; left += 1) {
    for (let right = left + 1; right < items.length; right += 1) {
      const a = items[left];
      const b = items[right];
      if (a !== undefined && b !== undefined && layoutsCollide(a, b)) {
        issues.push(`${a.instanceId} overlaps ${b.instanceId}`);
      }
    }
  }
  return issues;
};

const reject = (
  items: DashboardLayout,
  reason: NonNullable<LayoutMutationResult["reason"]>,
): LayoutMutationResult => ({ accepted: false, items, reason });

const replaceItem = (
  items: DashboardLayout,
  replacement: WidgetLayoutItem,
): DashboardLayout =>
  items.map((item) => (item.instanceId === replacement.instanceId ? replacement : item));

const validateReplacement = (
  items: DashboardLayout,
  current: WidgetLayoutItem,
  replacement: WidgetLayoutItem,
): LayoutMutationResult => {
  if (!isLayoutItemWithinBounds(replacement)) {
    return reject(items, "out-of-bounds");
  }
  if (!isLayoutItemWithinSizeLimits(replacement)) {
    return reject(items, "size-limit");
  }
  const siblings = items.filter((item) => item.instanceId !== current.instanceId);
  if (hasLayoutCollision(replacement, siblings)) {
    return reject(items, "collision");
  }
  return { accepted: true, items: replaceItem(items, replacement) };
};

export const moveLayoutItem = (
  items: DashboardLayout,
  instanceId: WidgetInstanceId,
  x: number,
  y: number,
): LayoutMutationResult => {
  const item = items.find((candidate) => candidate.instanceId === instanceId);
  if (item === undefined) return reject(items, "not-found");
  if (item.positionLocked === true) return reject(items, "locked");
  return validateReplacement(items, item, { ...item, x, y });
};

export const resizeLayoutItem = (
  items: DashboardLayout,
  instanceId: WidgetInstanceId,
  w: number,
  h: number,
): LayoutMutationResult => {
  const item = items.find((candidate) => candidate.instanceId === instanceId);
  if (item === undefined) return reject(items, "not-found");
  if (item.sizeLocked === true) return reject(items, "locked");
  return validateReplacement(items, item, { ...item, w, h });
};

export const applyLayoutCommand = (
  items: DashboardLayout,
  command: LayoutCommand,
): LayoutMutationResult => {
  const item = items.find((candidate) => candidate.instanceId === command.instanceId);
  if (item === undefined) return reject(items, "not-found");
  const amount = Math.max(1, Math.floor(command.amount ?? 1));
  if (command.kind === "move") {
    const deltas: Record<typeof command.direction, readonly [number, number]> = {
      left: [-amount, 0],
      right: [amount, 0],
      up: [0, -amount],
      down: [0, amount],
    };
    const delta = deltas[command.direction];
    return moveLayoutItem(items, item.instanceId, item.x + delta[0], item.y + delta[1]);
  }

  const deltas: Record<typeof command.direction, readonly [number, number]> = {
    "grow-width": [amount, 0],
    "shrink-width": [-amount, 0],
    "grow-height": [0, amount],
    "shrink-height": [0, -amount],
  };
  const next = deltas[command.direction];
  return resizeLayoutItem(items, item.instanceId, item.w + next[0], item.h + next[1]);
};

export interface PlacementOptions {
  readonly preferred?: { readonly x: number; readonly y: number };
  readonly columns?: number;
}

/** Stable row-major first-fit used by click-to-add and occupied-slot restoration. */
export const findFirstAvailablePlacement = (
  items: DashboardLayout,
  size: GridSize,
  options: PlacementOptions = {},
): { readonly x: number; readonly y: number } => {
  const columns = options.columns ?? DASHBOARD_COLUMNS;
  if (size.w <= 0 || size.h <= 0 || size.w > columns) {
    throw new Error(`Invalid placement size ${size.w}x${size.h} for ${columns} columns`);
  }
  const preferred = options.preferred ?? { x: 0, y: 0 };
  const maxBottom = items.reduce((bottom, item) => Math.max(bottom, item.y + item.h), 0);
  const finalRow = Math.max(preferred.y, maxBottom) + size.h + 1;

  for (let y = Math.max(0, preferred.y); y <= finalRow; y += 1) {
    const startX = y === preferred.y ? Math.max(0, preferred.x) : 0;
    for (let x = startX; x <= columns - size.w; x += 1) {
      const candidate: WidgetLayoutItem = {
        instanceId: "__placement__" as WidgetInstanceId,
        x,
        y,
        w: size.w,
        h: size.h,
      };
      if (!hasLayoutCollision(candidate, items)) return { x, y };
    }
  }
  return { x: 0, y: finalRow + 1 };
};

export const addLayoutItem = (
  items: DashboardLayout,
  item: WidgetLayoutItem,
): LayoutMutationResult => {
  if (items.some((candidate) => candidate.instanceId === item.instanceId)) {
    return reject(items, "collision");
  }
  if (!isLayoutItemWithinSizeLimits(item)) return reject(items, "size-limit");
  const placement =
    isLayoutItemWithinBounds(item) && !hasLayoutCollision(item, items)
      ? { x: item.x, y: item.y }
      : findFirstAvailablePlacement(items, item, { preferred: item });
  const placed = { ...item, ...placement };
  return { accepted: true, items: [...items, placed] };
};

export const tidyDashboardLayout = (items: DashboardLayout): DashboardLayout => {
  const byId = new Map(items.map((item) => [item.instanceId, item]));
  const compacted = verticalCompactor.compact(
    items.map((item) => ({
      i: item.instanceId,
      x: item.x,
      y: item.y,
      w: item.w,
      h: item.h,
      ...(item.minW === undefined ? {} : { minW: item.minW }),
      ...(item.maxW === undefined ? {} : { maxW: item.maxW }),
      ...(item.minH === undefined ? {} : { minH: item.minH }),
      ...(item.maxH === undefined ? {} : { maxH: item.maxH }),
      ...(item.positionLocked === true ? { static: true } : {}),
    })) satisfies RglLayout,
    DASHBOARD_COLUMNS,
  );
  return compacted.map((item) => {
    const original = byId.get(item.i as WidgetInstanceId);
    if (original === undefined) throw new Error(`Tidy returned unknown item ${item.i}`);
    return { ...original, x: item.x, y: item.y, w: item.w, h: item.h };
  });
};
