import {
  lazy,
  type ComponentType,
  Suspense,
  useMemo,
} from "react";

import { useTheme } from "../../theme/ThemeProvider";
import type {
  DefaultWidgetSlot,
  ViewId,
  WidgetDefinition,
  WidgetInstanceId,
  WidgetIntent,
  WidgetModule,
  WidgetRendererProps,
  WidgetSizeMode,
} from "../contributions/contracts";
import { WidgetErrorBoundary } from "./WidgetErrorBoundary";
import { WidgetFrame } from "./WidgetFrame";
import { WidgetMenu } from "./WidgetMenu";
import {
  type WidgetHostStatus,
  WidgetState,
  WidgetStatusBanner,
} from "./WidgetStates";

type BoundWidgetRenderer = ComponentType<WidgetRendererProps<unknown, WidgetIntent>>;

const supportedRendererSymbols = new Set([
  Symbol.for("react.memo"),
  Symbol.for("react.forward_ref"),
  Symbol.for("react.lazy"),
]);

const isBoundWidgetRenderer = (value: unknown): value is BoundWidgetRenderer =>
  typeof value === "function" ||
  (typeof value === "object" &&
    value !== null &&
    "$$typeof" in value &&
    supportedRendererSymbols.has((value as { $$typeof: symbol }).$$typeof));

const blockingStates = new Set<WidgetHostStatus>([
  "loading",
  "empty",
  "unavailable",
  "permission-denied",
  "error",
]);

const contextualStates = new Set<WidgetHostStatus>([
  "stale",
  "offline",
  "read-only",
]);

export interface WidgetHostProps<Input = unknown> {
  readonly definition: WidgetDefinition;
  readonly module: WidgetModule;
  readonly instanceId: WidgetInstanceId;
  readonly viewId: ViewId;
  readonly input?: Input;
  readonly status: WidgetHostStatus;
  readonly statusMessage?: string;
  readonly width: number;
  readonly height: number;
  readonly sizeMode: WidgetSizeMode;
  readonly editing: boolean;
  emit(intent: WidgetIntent): void;
  readonly presence?: DefaultWidgetSlot["presence"];
  readonly lockedReason?: string;
  readonly onRetry?: () => void;
  readonly onConfigure?: () => void;
  readonly onHide?: () => void;
  readonly onRemove?: () => void;
  readonly onRendererError?: (error: Error) => void;
}

export function WidgetHost<Input>({
  definition,
  module,
  instanceId,
  viewId,
  input,
  status,
  statusMessage,
  width,
  height,
  sizeMode,
  editing,
  emit,
  presence,
  lockedReason,
  onRetry,
  onConfigure,
  onHide,
  onRemove,
  onRendererError,
}: WidgetHostProps<Input>) {
  const themeRuntime = useTheme();
  const Renderer = useMemo(
    () =>
      lazy(async () => {
        if (module.widgetTypeId !== definition.typeId) {
          throw new Error(
            `Renderer module ${module.moduleId} does not belong to ${definition.typeId}`,
          );
        }
        const loaded = await module.load();
        if (!isBoundWidgetRenderer(loaded.default)) {
          throw new Error(
            `Widget module ${module.moduleId} has no valid React renderer export`,
          );
        }
        return { default: loaded.default };
      }),
    [definition.typeId, module],
  );
  const presentation = useMemo(
    () => ({
      instanceId,
      viewId,
      width,
      height,
      sizeMode,
      editing,
      theme: themeRuntime.theme,
      getCanvasTheme: themeRuntime.getCanvasTheme,
    }),
    [
      editing,
      height,
      instanceId,
      sizeMode,
      themeRuntime,
      viewId,
      width,
    ],
  );

  const menu = (
    <WidgetMenu
      widgetTitle={definition.displayName}
      presence={presence}
      lockedReason={lockedReason}
      onRetry={onRetry}
      onConfigure={onConfigure}
      onHide={onHide}
      onRemove={onRemove}
    />
  );
  const body = blockingStates.has(status) ? (
    <WidgetState state={status} message={statusMessage} onRetry={onRetry} />
  ) : (
    <WidgetErrorBoundary
      resetKey={`${module.moduleId}:${instanceId}`}
      onRetry={onRetry}
      onError={(error) => onRendererError?.(error)}
    >
      <Suspense fallback={<WidgetState state="loading" />}>
        <Renderer input={input} emit={emit} presentation={presentation} />
      </Suspense>
    </WidgetErrorBoundary>
  );

  return (
    <WidgetFrame
      title={definition.displayName}
      menu={menu}
      busy={status === "loading"}
      status={
        contextualStates.has(status) ? (
          <WidgetStatusBanner state={status} message={statusMessage} />
        ) : undefined
      }
    >
      {body}
    </WidgetFrame>
  );
}
