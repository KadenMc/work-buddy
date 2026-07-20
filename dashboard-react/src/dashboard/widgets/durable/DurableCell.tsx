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
  return (
    <div
      className="wb-durable-cell"
      data-durable-cell-for={instanceId}
      ref={(cell: HTMLDivElement | null) => {
        if (cell === null) {
          return;
        }
        adopt(instanceId, cell);
        return () => {
          release(instanceId, cell);
        };
      }}
    />
  );
}
