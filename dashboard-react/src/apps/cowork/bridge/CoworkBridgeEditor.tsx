import { useEffect, useMemo, useRef, useState } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import type { Editor } from "@tiptap/core";
import * as Y from "yjs";

import { CoworkYdocPersistence } from "../persistence/CoworkYdocPersistence";
import type { CoworkYdocTransport } from "../persistence/transport";
import {
  buildEditorExtensions,
  stopCapturingLoadTimeIds,
} from "../editor/extensions";
import { importCoworkMarkdown } from "../editor/markdownImport";
import type { WbTrackedChangesAdapter } from "../suggestions/types";
import "./styles.css";

/** What the host reports up once the editor is mounted and the adapter attached. */
export interface CoworkEditorReadyContext {
  readonly editor: Editor;
  /** The ProseMirror DOM root, the coordinate source for the anchor-rect measurements. */
  readonly dom: HTMLElement;
}

export interface CoworkBridgeEditorProps {
  /** The shared local Y.Doc the adapter is bound to (apply-origin tagging, section 1.4). */
  readonly document: Y.Doc;
  /** The tracked-change adapter, attached to the editor here and shared with the bridge. */
  readonly adapter: WbTrackedChangesAdapter;
  /** The Yjs transport (HttpCoworkYdocTransport live, in-memory in tests). */
  readonly transport: CoworkYdocTransport;
  /** Markdown seeded into a brand-new document exactly once, on an empty fragment. */
  readonly seedMarkdown: string;
  /** Fired once the editor is mounted and the adapter attached. */
  readonly onReady?: (context: CoworkEditorReadyContext) => void;
  /** Fired when the editor is about to unmount, so the bridge can drop its editor refs. */
  readonly onTeardown?: () => void;
}

interface MountedProps extends CoworkBridgeEditorProps {
  readonly persistence: CoworkYdocPersistence;
  readonly seedWhenEmpty: boolean;
}

/**
 * The mounted live editor. It follows the same load-order the demo pane proved (SP-2): the
 * Y.Doc is hydrated from the transport before mount (the parent gates on that), the editor
 * binds to it, persistence starts pushing local human edits, a brand-new document is seeded
 * once, and the load-time id mint is fenced out of the undo stack. On top of the demo pane it
 * attaches the tracked-change adapter to the editor and reports the ready context up, so the
 * bridge can ingest proposals and measure anchor geometry.
 */
function MountedBridgeEditor({
  document,
  adapter,
  persistence,
  seedMarkdown,
  seedWhenEmpty,
  onReady,
  onTeardown,
}: MountedProps) {
  const extensions = useMemo(() => buildEditorExtensions(document), [document]);
  const seedContent = useMemo(
    () => importCoworkMarkdown(seedMarkdown).doc,
    [seedMarkdown],
  );
  const boundRef = useRef(false);

  const editor = useEditor(
    {
      extensions,
      immediatelyRender: false,
      editorProps: {
        attributes: {
          class: "wb-cowork-editor__surface",
          "aria-label": "Document editor",
          role: "textbox",
          "aria-multiline": "true",
        },
      },
    },
    [extensions],
  );

  useEffect(() => {
    if (editor === null || boundRef.current) return;
    boundRef.current = true;
    // Attach the adapter before seeding, so the tracked-change layer is ready the moment the
    // first proposal is ingested. Persistence starts before seeding so a brand-new document's
    // seed is pushed through R4 as its first human-origin update (SP-2 load-order).
    adapter.attach(editor);
    persistence.start();
    if (seedWhenEmpty) {
      editor.commands.setContent(seedContent);
    }
    stopCapturingLoadTimeIds(editor);
    onReady?.({ editor, dom: editor.view.dom as HTMLElement });
  }, [editor, adapter, persistence, seedContent, seedWhenEmpty, onReady]);

  useEffect(() => {
    return () => {
      onTeardown?.();
      adapter.detach();
    };
  }, [adapter, onTeardown]);

  return <EditorContent editor={editor} className="wb-cowork-editor__content" />;
}

/**
 * The live editor region of the Co-work surface. It owns its Yjs persistence controller,
 * hydrates the shared Y.Doc from the transport BEFORE mounting the editor, and gates the
 * mount by conditionally rendering (never useEditor(null), F5.4). The Y.Doc and the adapter
 * are passed in by the bridge so the review provider's submit path shares the same adapter.
 */
export function CoworkBridgeEditor(props: CoworkBridgeEditorProps) {
  const [persistence] = useState(
    () => new CoworkYdocPersistence(props.document, props.transport),
  );
  const [hydration, setHydration] = useState<{ readonly wasEmpty: boolean }>();

  useEffect(() => {
    let active = true;
    void persistence.hydrate().then((result) => {
      if (active) setHydration(result);
    });
    return () => {
      active = false;
      persistence.stop();
    };
  }, [persistence]);

  return (
    <section className="wb-cowork-editor" aria-label="Editor">
      {hydration !== undefined ? (
        <MountedBridgeEditor
          {...props}
          persistence={persistence}
          seedWhenEmpty={hydration.wasEmpty}
        />
      ) : (
        <p className="wb-cowork-editor__loading" role="status">
          Loading the document.
        </p>
      )}
    </section>
  );
}

export default CoworkBridgeEditor;
