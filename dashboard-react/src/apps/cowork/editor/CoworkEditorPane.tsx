import { useEffect, useMemo, useRef, useState } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import * as Y from "yjs";

import { CoworkYdocPersistence } from "../persistence/CoworkYdocPersistence";
import { LocalCoworkYdocTransport } from "../persistence/LocalCoworkYdocTransport";
import type { CoworkYdocTransport } from "../persistence/transport";
import { isLocalHumanOrigin } from "./applyOrigin";
import { buildEditorExtensions, stopCapturingLoadTimeIds } from "./extensions";
import { importCoworkMarkdown } from "./markdownImport";

// A brand-new document opens empty and honest. What the pane IS (its load-order contract,
// its review layer, its block-splice materialize) is documented as hover help on the editor
// region, not seeded as document content. Modes that want seeded prose pass it explicitly.
const DEFAULT_SEED_MARKDOWN = "";

// A workspace with no supplied document id persists to one stable scratch key, so
// widget-lab and any caller that omits the id still get reload-surviving local storage.
const DEFAULT_DOCUMENT_ID = "cowork-empty";

// Compaction folds the append log into a single snapshot once edits go idle, keeping the
// persisted form small without the transport ever interpreting the Yjs bytes.
const COMPACTION_IDLE_MS = 2_000;

export interface CoworkEditorPaneProps {
  /**
   * Identifies the persisted document, so its local Yjs state is keyed per document and
   * survives reload. The surface passes the workspace's document id here. Defaults to a
   * stable empty-workspace key when omitted.
   */
  readonly documentId?: string;
  /** Markdown seeded into a brand-new document exactly once, on an empty fragment. */
  readonly seedMarkdown?: string;
  /** Injectable for tests; defaults to a fresh local Y.Doc. */
  readonly document?: Y.Doc;
  /** Injectable for tests; defaults to the reload-surviving local transport. */
  readonly transport?: CoworkYdocTransport;
}

interface MountedCoworkEditorProps {
  readonly document: Y.Doc;
  readonly persistence: CoworkYdocPersistence;
  readonly seedMarkdown: string;
  readonly seedWhenEmpty: boolean;
}

/**
 * The mounted editor. Only rendered once the Y.Doc has been hydrated from persistence,
 * so the editor is never bound to a Y.Doc that will be populated later (SP-2 point 1).
 * The editor `content` option is discarded under Collaboration (F1.4), so a brand-new
 * document is seeded once via a post-mount `setContent`. Seeding keys off what
 * persistence pulled (`seedWhenEmpty`) rather than a post-mount fragment-emptiness check,
 * because the editor's own empty-doc sync can make the fragment non-empty first. The
 * load-time id mint is fenced out of the undo stack with `stopCapturing` (point 4), and
 * only then does persistence begin pushing local edits.
 */
function MountedCoworkEditor({
  document,
  persistence,
  seedMarkdown,
  seedWhenEmpty,
}: MountedCoworkEditorProps) {
  const extensions = useMemo(() => buildEditorExtensions(document), [document]);
  // An empty seed means a genuinely empty document, so nothing is parsed or set and the
  // editor opens on its own empty state rather than fabricated placeholder prose.
  const seedContent = useMemo(
    () =>
      seedMarkdown.trim().length > 0 ? importCoworkMarkdown(seedMarkdown).doc : null,
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
    // Attach the push observer BEFORE seeding, so a brand-new document's initial
    // content is pushed through R4 as its first human-origin update. Seeding after
    // start() (rather than before) means a second client hydrating from the server
    // sees the seed instead of orphaned updates that reference an unpushed base (S2).
    persistence.start();
    if (seedWhenEmpty && seedContent !== null) {
      editor.commands.setContent(seedContent);
    }
    stopCapturingLoadTimeIds(editor);
    // The collaborative binding synchronizes the editor's base structure into
    // the document while the editor is being created, before start() attached
    // the push observer, so a brand-new document's update log would reference a
    // base that persistence never saw and a reload would rehydrate to nothing.
    // One immediate compaction stores the complete current state as the
    // snapshot, anchoring every later log entry, so a reload at any moment
    // restores the document instead of orphaned updates over a missing base.
    void persistence.compact();
  }, [editor, persistence, seedContent, seedWhenEmpty]);

  // Keep the persisted append log bounded. A human edit reschedules an idle-debounced
  // compaction, and a reload or tab close flushes one immediately through pagehide, so
  // the stored form stays compact across sessions.
  useEffect(() => {
    let idleTimer: ReturnType<typeof setTimeout> | undefined;
    const cancelPending = (): void => {
      if (idleTimer !== undefined) {
        clearTimeout(idleTimer);
        idleTimer = undefined;
      }
    };
    const scheduleCompaction = (): void => {
      cancelPending();
      idleTimer = setTimeout(() => {
        idleTimer = undefined;
        void persistence.compact();
      }, COMPACTION_IDLE_MS);
    };
    const onDocUpdate = (_update: Uint8Array, origin: unknown): void => {
      // Only a human edit grows the log, so only that reschedules a compaction.
      if (isLocalHumanOrigin(origin)) scheduleCompaction();
    };
    const onPageHide = (): void => {
      cancelPending();
      void persistence.compact();
    };
    document.on("update", onDocUpdate);
    const hasWindow = typeof window !== "undefined";
    if (hasWindow) window.addEventListener("pagehide", onPageHide);
    return () => {
      cancelPending();
      document.off("update", onDocUpdate);
      if (hasWindow) window.removeEventListener("pagehide", onPageHide);
    };
  }, [document, persistence]);

  return <EditorContent editor={editor} className="wb-cowork-editor__content" />;
}

/**
 * The editor region of the Co-work surface. Owns the live local Y.Doc and its
 * persistence controller, hydrates from the transport BEFORE mounting the editor, and
 * gates the editor mount by conditionally rendering it (never `useEditor(null)`, F5.4).
 */
export function CoworkEditorPane({
  documentId,
  seedMarkdown = DEFAULT_SEED_MARKDOWN,
  document,
  transport,
}: CoworkEditorPaneProps) {
  const [doc] = useState(() => document ?? new Y.Doc());
  const [store] = useState(
    () =>
      transport ??
      new LocalCoworkYdocTransport({ documentId: documentId ?? DEFAULT_DOCUMENT_ID }),
  );
  const [persistence] = useState(() => new CoworkYdocPersistence(doc, store));
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
        <MountedCoworkEditor
          document={doc}
          persistence={persistence}
          seedMarkdown={seedMarkdown}
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

export default CoworkEditorPane;
