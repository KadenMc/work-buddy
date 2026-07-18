import { Extension } from "@tiptap/core";

import { suggestChanges } from "./engine";

/**
 * Hosts the vendored suggest-changes ProseMirror plugin as one Tiptap extension. The
 * plugin contributes the block-boundary deletion / insertion widget decorations and the
 * zero-width-space arrow-key handling (engine plugin.ts). Suggest mode stays disabled
 * in v1: human edits are direct and only agent proposals become suggestions, which the
 * adapter ingests programmatically through transformToSuggestionTransaction. The plugin
 * is present so the decoration layer and the guarded dispatch decorator have a home.
 */
export const CoworkSuggestChanges = Extension.create({
  name: "coworkSuggestChanges",

  addProseMirrorPlugins() {
    return [suggestChanges()];
  },
});
