/**
 * Vendored from @handlewithcare/prosemirror-suggest-changes v0.1.8 (MIT).
 * Upstream https://github.com/handlewithcarecollective/prosemirror-suggest-changes
 * See the LICENSE and PROVENANCE.md files alongside this source.
 *
 * Modifications in this file: import specifiers only. Bare prosemirror-* imports were
 * rewritten to the @tiptap/pm/* subpaths that resolve the single hoisted ProseMirror
 * instance in this tree, and relative .js extensions were dropped to match the
 * dashboard-react bundler module resolution.
 */

import {
  type EditorState,
  Plugin,
  PluginKey,
  TextSelection,
} from "@tiptap/pm/state";
import { getSuggestionDecorations } from "./decorations";

export const suggestChangesKey = new PluginKey<{ enabled: boolean }>(
  "@handlewithcare/prosemirror-suggest-changes",
);

export function suggestChanges() {
  return new Plugin<{ enabled: boolean }>({
    key: suggestChangesKey,
    state: {
      init() {
        return { enabled: false };
      },
      apply(tr, value) {
        const meta = tr.getMeta(suggestChangesKey) as
          | { enabled: boolean }
          | { skip: true }
          | undefined;
        if (meta && "enabled" in meta) return meta;
        return value;
      },
    },
    props: {
      decorations: getSuggestionDecorations,
      // Add a custom keydown handler that skips over any zero-width
      // spaces that we've inserted so that users aren't aware of them
      handleKeyDown(view, event) {
        if (
          event.key === "ArrowRight" &&
          view.state.selection instanceof TextSelection &&
          view.state.selection.empty &&
          view.state.selection.$cursor?.nodeAfter?.text?.startsWith("\u200B")
        ) {
          view.dispatch(
            view.state.tr.setSelection(
              TextSelection.create(
                view.state.doc,
                view.state.selection.$cursor.pos + 1,
              ),
            ),
          );
        }

        if (
          event.key === "ArrowLeft" &&
          view.state.selection instanceof TextSelection &&
          view.state.selection.empty &&
          view.state.selection.$cursor?.nodeBefore?.text?.endsWith("\u200B")
        ) {
          view.dispatch(
            view.state.tr.setSelection(
              TextSelection.create(
                view.state.doc,
                view.state.selection.$cursor.pos - 1,
              ),
            ),
          );
        }

        // Never block any other handlers from running after
        return false;
      },
    },
  });
}

export function isSuggestChangesEnabled(state: EditorState) {
  return !!suggestChangesKey.getState(state)?.enabled;
}
