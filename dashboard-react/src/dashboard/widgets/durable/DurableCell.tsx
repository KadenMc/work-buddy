import { useCallback } from "react";

import type { WidgetInstanceId } from "../../contributions/contracts";
import { useDurableHost } from "./durableContext";

export interface DurableCellProps {
  readonly instanceId: WidgetInstanceId;
}

/**
 * A light placeholder that marks where a durable widget belongs in the grid. It
 * renders an empty div and, through the React ref cleanup form, asks the host to
 * move the widget's permanent wrapper into that div on mount and to take it back
 * on unmount. The cell holds no widget state of its own, so the grid may remount
 * it as often as it likes with no effect on the live widget above.
 */
export function DurableCell({ instanceId }: DurableCellProps) {
  const { adopt, release } = useDurableHost();
  // React cleans up a changed callback ref before invoking its replacement.
  // Keep this ref stable across ordinary parent renders so a durable widget is
  // only parked for a real cell unmount or instance/host identity change.
  const attachCell = useCallback(
    (cell: HTMLDivElement | null) => {
      if (cell === null) {
        return;
      }
      adopt(instanceId, cell);
      return () => {
        release(instanceId, cell);
      };
    },
    [adopt, instanceId, release],
  );

  return (
    <div
      className="wb-durable-cell"
      data-durable-cell-for={instanceId}
      ref={attachCell}
    />
  );
}
