import { createContext, useContext } from "react";

import type { WidgetInstanceId } from "../../contributions/contracts";

/**
 * The keep-alive handle a DurableWidgetHost publishes to the placeholder cells
 * beneath it. A cell calls adopt when it mounts to pull its permanent wrapper
 * into place, and release when it goes away to park that wrapper back offstage.
 * Both take the live cell element so the host can guard every move by parent
 * identity and stay correct when React double-invokes a mount.
 */
export interface DurableHostHandle {
  adopt(instanceId: WidgetInstanceId, cell: HTMLElement): void;
  release(instanceId: WidgetInstanceId, cell: HTMLElement): void;
}

/**
 * The default handle does nothing, so a DurableCell rendered with no host above
 * it simply shows its empty placeholder div and never throws.
 */
const inertHostHandle: DurableHostHandle = {
  adopt() {},
  release() {},
};

export const DurableHostContext =
  createContext<DurableHostHandle>(inertHostHandle);

/** Read the nearest keep-alive host handle, or the inert default when unhosted. */
export function useDurableHost(): DurableHostHandle {
  return useContext(DurableHostContext);
}
