import { Extension, type Editor } from "@tiptap/core";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { Plugin, PluginKey, type Transaction } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";

import type { CoworkProseMirrorRange } from "../targets/contracts";

export interface CoworkWorkingTargetDecoration
  extends CoworkProseMirrorRange {
  readonly label: string;
}

export interface CoworkWorkingTargetStartDecoration {
  readonly position: number;
  readonly label: string;
}

type CoworkWorkingTargetProjection =
  | {
      readonly kind: "target";
      readonly target: CoworkWorkingTargetDecoration;
    }
  | {
      readonly kind: "provisional_start";
      readonly start: CoworkWorkingTargetStartDecoration;
    };

interface CoworkWorkingTargetDecorationState {
  readonly projection: CoworkWorkingTargetProjection | null;
  readonly decorations: DecorationSet;
}

type CoworkWorkingTargetDecorationMeta =
  | {
      readonly type: "project";
      readonly target: CoworkWorkingTargetDecoration;
    }
  | {
      readonly type: "project_provisional_start";
      readonly start: CoworkWorkingTargetStartDecoration;
    }
  | { readonly type: "clear" };

export const coworkWorkingTargetDecorationsKey =
  new PluginKey<CoworkWorkingTargetDecorationState>(
    "coworkWorkingTargetDecorations",
  );

const boundaryWidget = (
  position: number,
  boundary: "start" | "end",
  label: string,
  provisional = false,
): Decoration =>
  Decoration.widget(
    position,
    () => {
      const element = document.createElement("span");
      element.className = [
        "wb-cowork-working-target__boundary",
        `wb-cowork-working-target__boundary--${boundary}`,
        provisional
          ? "wb-cowork-working-target__boundary--provisional"
          : "",
      ]
        .filter(Boolean)
        .join(" ");
      element.dataset.wbWorkingTargetBoundary = boundary;
      element.dataset.wbWorkingTargetLabel = label;
      if (provisional) {
        element.dataset.wbWorkingTargetProvisional = "true";
      }
      element.setAttribute("contenteditable", "false");
      element.setAttribute("aria-hidden", "true");
      return element;
    },
    {
      key: [
        "working-target",
        provisional ? "provisional" : "resolved",
        boundary,
        String(position),
        label,
      ].join(":"),
      side: boundary === "start" ? -1 : 1,
    },
  );

const buildDecorations = (
  doc: ProseMirrorNode,
  projection: CoworkWorkingTargetProjection | null,
): DecorationSet => {
  if (projection?.kind === "provisional_start") {
    const { start } = projection;
    if (start.position < 0 || start.position > doc.content.size) {
      return DecorationSet.empty;
    }
    return DecorationSet.create(doc, [
      boundaryWidget(start.position, "start", start.label, true),
    ]);
  }
  const target = projection?.kind === "target" ? projection.target : null;
  if (
    target === null ||
    target.from < 0 ||
    target.to > doc.content.size ||
    target.to <= target.from
  ) {
    return DecorationSet.empty;
  }
  return DecorationSet.create(doc, [
    Decoration.inline(
      target.from,
      target.to,
      {
        class: "wb-cowork-working-target__highlight",
        "data-wb-working-target": "true",
        "data-wb-working-target-label": target.label,
      },
      {
        key: `working-target:highlight:${target.label}`,
        inclusiveStart: false,
        inclusiveEnd: false,
      },
    ),
    boundaryWidget(target.from, "start", target.label),
    boundaryWidget(target.to, "end", target.label),
  ]);
};

const mappedProjection = (
  transaction: Transaction,
  projection: CoworkWorkingTargetProjection | null,
): CoworkWorkingTargetProjection | null => {
  if (projection === null || !transaction.docChanged) return projection;
  if (projection.kind === "provisional_start") {
    const position = transaction.mapping.mapResult(
      projection.start.position,
      1,
    );
    return position.deletedAcross
      ? null
      : {
          kind: "provisional_start",
          start: { ...projection.start, position: position.pos },
        };
  }
  const target = projection.target;
  const from = transaction.mapping.mapResult(target.from, 1);
  const to = transaction.mapping.mapResult(target.to, -1);
  if (from.deletedAcross || to.deletedAcross || from.pos >= to.pos) return null;
  return {
    kind: "target",
    target: { ...target, from: from.pos, to: to.pos },
  };
};

export function coworkWorkingTargetDecorationsPlugin(): Plugin<CoworkWorkingTargetDecorationState> {
  return new Plugin<CoworkWorkingTargetDecorationState>({
    key: coworkWorkingTargetDecorationsKey,
    state: {
      init: () => ({
        projection: null,
        decorations: DecorationSet.empty,
      }),
      apply(transaction, value) {
        const meta = transaction.getMeta(
          coworkWorkingTargetDecorationsKey,
        ) as CoworkWorkingTargetDecorationMeta | undefined;
        if (meta === undefined) {
          if (!transaction.docChanged) return value;
          const projection = mappedProjection(
            transaction,
            value.projection,
          );
          return {
            projection,
            decorations:
              projection === null
                ? DecorationSet.empty
                : value.decorations.map(
                    transaction.mapping,
                    transaction.doc,
            ),
          };
        }
        const projection: CoworkWorkingTargetProjection | null =
          meta.type === "project"
            ? { kind: "target", target: meta.target }
            : meta.type === "project_provisional_start"
              ? { kind: "provisional_start", start: meta.start }
              : null;
        return {
          projection,
          decorations: buildDecorations(transaction.doc, projection),
        };
      },
    },
    props: {
      decorations(state) {
        return (
          coworkWorkingTargetDecorationsKey.getState(state)?.decorations ??
          null
        );
      },
    },
  });
}

/**
 * Runtime-only Working on channel. It is deliberately separate from Review
 * focus and transient Chat passage highlighting.
 */
export const CoworkWorkingTargetDecorations = Extension.create({
  name: "coworkWorkingTargetDecorations",

  addProseMirrorPlugins() {
    return [coworkWorkingTargetDecorationsPlugin()];
  },
});

const dispatchMeta = (
  editor: Editor,
  meta: CoworkWorkingTargetDecorationMeta,
): boolean => {
  if (
    editor.isDestroyed ||
    coworkWorkingTargetDecorationsKey.getState(editor.state) === undefined
  ) {
    return false;
  }
  editor.view.dispatch(
    editor.state.tr.setMeta(coworkWorkingTargetDecorationsKey, meta),
  );
  return true;
};

export const projectCoworkWorkingTarget = (
  editor: Editor,
  target: CoworkWorkingTargetDecoration,
): boolean => dispatchMeta(editor, { type: "project", target });

export const projectCoworkWorkingTargetStart = (
  editor: Editor,
  start: CoworkWorkingTargetStartDecoration,
): boolean =>
  dispatchMeta(editor, { type: "project_provisional_start", start });

export const clearCoworkWorkingTarget = (editor: Editor): boolean =>
  dispatchMeta(editor, { type: "clear" });

/** Read-only diagnostic projection for focused tests and bridge inspection. */
export const readCoworkWorkingTarget = (
  editor: Editor,
): CoworkWorkingTargetDecoration | null => {
  const projection = coworkWorkingTargetDecorationsKey.getState(
    editor.state,
  )?.projection;
  return projection?.kind === "target" ? projection.target : null;
};

/** Read-only diagnostic for the incomplete Set by cursor boundary. */
export const readCoworkWorkingTargetStart = (
  editor: Editor,
): CoworkWorkingTargetStartDecoration | null => {
  const projection = coworkWorkingTargetDecorationsKey.getState(
    editor.state,
  )?.projection;
  return projection?.kind === "provisional_start" ? projection.start : null;
};
