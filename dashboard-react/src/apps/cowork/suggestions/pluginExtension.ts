import { Extension } from "@tiptap/core";

import { suggestChanges } from "./engine";

/**
 * Hosts the vendored suggest-changes ProseMirror plugin for isolated compatibility
 * transforms and migration recovery. Production pending proposals use
 * CoworkLedgerDecorations and do not pass through this plugin or its marks.
 */
export const CoworkSuggestChanges = Extension.create({
  name: "coworkSuggestChanges",

  addProseMirrorPlugins() {
    return [suggestChanges()];
  },
});
