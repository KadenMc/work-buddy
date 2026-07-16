import {
  useCallback,
  lazy,
  type ComponentType,
  Suspense,
  useMemo,
  useRef,
} from "react";
import { ClockCounterClockwise } from "@phosphor-icons/react/ClockCounterClockwise";
import { Eraser } from "@phosphor-icons/react/Eraser";
import { NotePencil } from "@phosphor-icons/react/NotePencil";
import { SquaresFour } from "@phosphor-icons/react/SquaresFour";
import { TextAlignLeft } from "@phosphor-icons/react/TextAlignLeft";

import { useTheme } from "../../theme/ThemeProvider";
import { IconButton } from "../../ui";
import {
  WidgetDraftScopeProvider,
  useWidgetDraftScopeStatus,
} from "../drafts";
import { useInteractionSurfaces } from "../interactions";
import { HelpTarget, type HelpContent } from "../help";
import type {
  DefaultWidgetSlot,
  GridSize,
  IntentResult,
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
  readonly interactionMode: "operate" | "arrange" | "preview";
  readonly gridSize?: GridSize;
  readonly help?: HelpContent;
  emit(intent: WidgetIntent): Promise<import("../contributions/contracts").IntentResult>;
  readonly presence?: DefaultWidgetSlot["presence"];
  readonly lockedReason?: string;
  readonly onRetry?: () => void;
  readonly onHide?: () => void;
  readonly onRemove?: () => void;
  readonly onRendererError?: (error: Error) => void;
}

/**
 * Clear is intentionally a direct, conditional header action while it is the
 * only truthful normal-mode action. If widgets gain multiple stable runtime
 * actions, promote them to a persistent runtime-actions menu rather than
 * conditionally hiding that multi-action menu with draft state.
 */
function WidgetClearDraftButton({
  widgetTitle,
  onCleared,
  onCanceled,
}: {
  readonly widgetTitle: string;
  onCleared(): void;
  onCanceled(): void;
}) {
  const { hasDirtyDraft, clearAll } = useWidgetDraftScopeStatus();
  const { confirm, notify } = useInteractionSurfaces();
  if (!hasDirtyDraft) return null;
  const clearDraft = async () => {
    const accepted = await confirm({
      title: "Clear this draft?",
      description: `Clear the unfinished ${widgetTitle} content on this device? Saved items, widget settings, and the view layout will not be affected.`,
      confirmLabel: "Clear draft",
      cancelLabel: "Keep draft",
      tone: "danger",
    });
    if (!accepted) {
      onCanceled();
      return;
    }
    const cleared = await clearAll();
    notify({
      message: cleared ? `${widgetTitle} draft cleared.` : `${widgetTitle} draft could not be cleared.`,
      tone: cleared ? "success" : "danger",
      dedupeKey: `widget-draft-clear:${widgetTitle}`,
    });
    if (cleared) onCleared();
  };
  return (
    <HelpTarget
      content={{
        summary: "Clear the unfinished working state for this widget.",
        details:
          "This removes the recoverable draft stored for this widget. It does not delete saved records, change widget settings, or alter the view layout.",
      }}
      placement="bottom end"
      reactAriaComposite
    >
    <IconButton
      label={`Clear ${widgetTitle} draft`}
      icon={<Eraser weight="duotone" />}
      variant="ghost"
      size="small"
      className="wb-widget-clear-draft"
      onClick={() => void clearDraft()}
    />
    </HelpTarget>
  );
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
  interactionMode,
  gridSize,
  help,
  emit,
  presence,
  lockedReason,
  onRetry,
  onHide,
  onRemove,
  onRendererError,
}: WidgetHostProps<Input>) {
  const themeRuntime = useTheme();
  const { notify } = useInteractionSurfaces();
  const frameRef = useRef<HTMLElement>(null);
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
      interactionMode,
      editing: interactionMode === "arrange",
      theme: themeRuntime.theme,
      getCanvasTheme: themeRuntime.getCanvasTheme,
    }),
    [
      height,
      interactionMode,
      instanceId,
      sizeMode,
      themeRuntime,
      viewId,
      width,
    ],
  );

  const dispatch = useCallback(
    async (intent: WidgetIntent): Promise<IntentResult> => {
      if (interactionMode === "operate") return emit(intent);
      if (interactionMode === "arrange") {
        return {
          intent_id: intent.intent_id,
          ...(intent.client_mutation_id === undefined
            ? {}
            : { client_mutation_id: intent.client_mutation_id }),
          status: "unavailable",
          message: "Widget actions are paused while arranging this view.",
        };
      }
      const declaration = definition.outputIntentEffects?.find(
        (candidate) =>
          candidate.schema.schemaId === intent.intent_type &&
          candidate.schema.version === intent.schema_version,
      );
      const simulated = declaration?.preview === "simulate";
      notify({
        message: simulated
          ? `${definition.displayName} previewed that action locally; nothing was saved.`
          : `${definition.displayName} did not run that action in Preview. Finish customizing to use it.`,
        tone: simulated ? "info" : "warning",
        dedupeKey: `widget-preview:${instanceId}:${intent.intent_type}`,
      });
      return {
        intent_id: intent.intent_id,
        ...(intent.client_mutation_id === undefined
          ? {}
          : { client_mutation_id: intent.client_mutation_id }),
        status: simulated ? "accepted" : "unavailable",
        message: simulated
          ? "Preview simulation only; no authoritative state changed."
          : "This action is unavailable in interaction Preview.",
      };
    },
    [definition.displayName, definition.outputIntentEffects, emit, instanceId, interactionMode, notify],
  );

  const menu = interactionMode === "arrange" ? (
    <WidgetMenu
      widgetTitle={definition.displayName}
      presence={presence}
      lockedReason={lockedReason}
      onHide={onHide}
      onRemove={onRemove}
    />
  ) : interactionMode === "operate" && definition.drafts && definition.drafts.length > 0 ? (
    <WidgetClearDraftButton
      widgetTitle={definition.displayName}
      onCleared={() => {
        window.requestAnimationFrame(() => {
          const primaryControl = frameRef.current?.querySelector<HTMLElement>(
            "textarea:not([disabled]), input:not([disabled]), button:not([disabled])",
          );
          primaryControl?.focus({ preventScroll: true });
        });
      }}
      onCanceled={() => {
        window.requestAnimationFrame(() => {
          frameRef.current
            ?.querySelector<HTMLButtonElement>(".wb-widget-clear-draft")
            ?.focus({ preventScroll: true });
        });
      }}
    />
  ) : undefined;
  const body = blockingStates.has(status) ? (
    <WidgetState
      state={status}
      message={statusMessage}
      onRetry={interactionMode === "operate" ? onRetry : undefined}
    />
  ) : (
    <WidgetErrorBoundary
      resetKey={`${module.moduleId}:${instanceId}`}
      onRetry={interactionMode === "operate" ? onRetry : undefined}
      onError={(error) => onRendererError?.(error)}
    >
      <Suspense fallback={<WidgetState state="loading" />}>
        <Renderer input={input} emit={dispatch} presentation={presentation} />
      </Suspense>
    </WidgetErrorBoundary>
  );

  const frame = (
    <WidgetFrame
      ref={frameRef}
      title={definition.displayName}
      help={help}
      interactionMode={interactionMode}
      icon={
        definition.libraryPath[0] === "Capture" ? (
          <NotePencil weight="duotone" />
        ) : definition.libraryPath[0] === "Time" ? (
          <ClockCounterClockwise weight="duotone" />
        ) : definition.libraryPath[0] === "Notes" ? (
          <TextAlignLeft weight="duotone" />
        ) : (
          <SquaresFour weight="duotone" />
        )
      }
      headerMeta={
        interactionMode === "arrange" && gridSize ? (
          <span className="wb-widget-frame__layout-size">
            {gridSize.w} × {gridSize.h} grid units
          </span>
        ) : undefined
      }
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
  return definition.drafts && definition.drafts.length > 0 && input !== undefined ? (
    <WidgetDraftScopeProvider
      key={`draft-scope:${interactionMode}`}
      definition={definition}
      viewId={viewId}
      instanceId={instanceId}
      input={input}
      persistenceMode={interactionMode === "preview" ? "ephemeral" : "normal"}
    >
      {frame}
    </WidgetDraftScopeProvider>
  ) : (
    frame
  );
}
