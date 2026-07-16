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

const swap = (
  order: readonly WidgetInstanceId[],
  index: number,
  offset: -1 | 1,
): readonly WidgetInstanceId[] => {
  const target = index + offset;
  if (target < 0 || target >= order.length) return order;
  const next = [...order];
  [next[index], next[target]] = [next[target]!, next[index]!];
  return next;
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

  return (
    <section className="wb-mobile-order-editor" aria-labelledby="wb-mobile-order-title">
      <header>
        <div>
          <h2 id="wb-mobile-order-title">Mobile order</h2>
          <p>Set the one-column reading, focus, and screen-reader order used on mobile.</p>
        </div>
        <button type="button" onClick={onClose}>Close</button>
      </header>
      <ol>
        {normalized.map((instanceId, index) => {
          const instance = byId.get(instanceId)!;
          const widget = registry.getWidget(instance.widgetTypeId);
          return (
            <li key={instanceId}>
              <span>
                <strong>{widget?.definition.displayName ?? instance.widgetTypeId}</strong>
              </span>
              <span className="wb-mobile-order-editor__actions">
                <button
                  type="button"
                  disabled={index === 0}
                  aria-label={`Move ${widget?.definition.displayName ?? instance.widgetTypeId} earlier on mobile`}
                  onClick={() => onChange(swap(normalized, index, -1))}
                >
                  Earlier
                </button>
                <button
                  type="button"
                  disabled={index === normalized.length - 1}
                  aria-label={`Move ${widget?.definition.displayName ?? instance.widgetTypeId} later on mobile`}
                  onClick={() => onChange(swap(normalized, index, 1))}
                >
                  Later
                </button>
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
