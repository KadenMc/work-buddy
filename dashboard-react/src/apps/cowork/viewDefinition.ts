import type { ViewDefinition } from "../../dashboard/contributions/contracts";
import type { WidgetThemeDeclaration } from "../../dashboard/contributions/themeContract";
import {
  COWORK_APP_ID,
  COWORK_ROUTE,
  COWORK_VIEW_ID,
  COWORK_WORKSPACE_INSTANCE_ID,
  COWORK_WORKSPACE_ROLE_ID,
  COWORK_WORKSPACE_SLOT_ID,
  COWORK_WORKSPACE_TYPE_ID,
} from "./bindings";

/**
 * Theme Contract v1 proof for the composite workspace card (section 5.4): full light,
 * dark, forced-colors, and reduced-motion support, styled through semantic tokens only. A
 * redundant non-color encoding for every trust state lives in the region components, not in
 * this manifest. The three regions the card composes share this one declaration.
 */
export const COWORK_WORKSPACE_THEME = {
  contractVersion: 1,
  conformance: "standard",
  supports: ["light", "dark", "forced-colors", "reduced-motion"],
  styling: "semantic-tokens",
} as const satisfies WidgetThemeDeclaration;

/**
 * The `wb.cowork.workspace` view (section 5). A standard 24-column grid that places one
 * composite durable widget: the workspace card carries the editor, the review rail, and the
 * health strip inside one live React tree. The slot is required and locked, so the card is
 * never removed and the shared live session is never split across the grid.
 */
export const COWORK_VIEW_DEFINITION = {
  viewId: COWORK_VIEW_ID,
  definitionVersion: 1,
  ownerAppId: COWORK_APP_ID,
  displayName: "Co-work",
  route: COWORK_ROUTE,
  navigation: {
    label: "Co-work",
    order: 30,
  },
  primaryJob:
    "Co-author a document with AI proposals the human reviews, one decision at a time.",
  grid: { columns: 24 },
  defaultSlots: [
    {
      slotId: COWORK_WORKSPACE_SLOT_ID,
      defaultInstanceId: COWORK_WORKSPACE_INSTANCE_ID,
      requiredRole: COWORK_WORKSPACE_ROLE_ID,
      defaultWidgetTypeId: COWORK_WORKSPACE_TYPE_ID,
      presence: "required",
      help: {
        summary: "Co-author the document with its review rail in one place.",
        details:
          "This required Co-work placement carries the editor, the aligned review rail, and the header health strip in one live workspace. It keeps the document, its tracked AI proposals, and the document conversation on one shared session so a decision made in the rail lands in the same editor beside it.",
      },
      lockedReason:
        "Without the workspace card, Co-work cannot co-author a document with its review rail on one shared session.",
      defaultSettings: {},
      defaultLayout: { x: 0, y: 0, w: 24, h: 20 },
      allowedSubstitution: { minimumDefinitionVersion: 1 },
    },
  ],
  readingOrder: [COWORK_WORKSPACE_SLOT_ID],
  mobileOrder: [COWORK_WORKSPACE_SLOT_ID],
} as const satisfies ViewDefinition;
