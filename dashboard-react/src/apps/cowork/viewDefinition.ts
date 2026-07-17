import type {
  SurfaceRegionDefinition,
  ViewDefinition,
} from "../../dashboard/contributions/contracts";
import type { WidgetThemeDeclaration } from "../../dashboard/contributions/themeContract";
import {
  COWORK_APP_ID,
  COWORK_REGION_IDS,
  COWORK_ROLE_IDS,
  COWORK_ROUTE,
  COWORK_VIEW_ID,
} from "./bindings";

/**
 * Theme Contract v1 proof shared by all three regions (section 5.4): each declares
 * full light, dark, forced-colors, and reduced-motion support and styles through
 * semantic tokens only. A redundant non-color encoding for every trust state lives in
 * the region components, not in this manifest.
 */
const COWORK_REGION_THEME = {
  contractVersion: 1,
  conformance: "standard",
  supports: ["light", "dark", "forced-colors", "reduced-motion"],
  styling: "semantic-tokens",
} as const satisfies WidgetThemeDeclaration;

const COWORK_REGIONS: readonly SurfaceRegionDefinition[] = [
  {
    regionId: COWORK_REGION_IDS.editor,
    role: COWORK_ROLE_IDS.editor,
    presence: "required",
    help: {
      summary: "Edit the document with tracked AI suggestions in view.",
      details:
        "The editor pane owns the live document and its suggestion decorations. It projects open proposals as an ephemeral review layer over the human-written text and materializes edits block by block.",
    },
    theme: COWORK_REGION_THEME,
  },
  {
    regionId: COWORK_REGION_IDS.reviewRail,
    role: COWORK_ROLE_IDS.reviewRail,
    presence: "required",
    help: {
      summary: "Review AI proposals and chat about the document beside it.",
      details:
        "The right rail carries a Review tab of aligned proposal cards and a Chat tab for the document conversation. It reads from the ledger and never mutates the editor directly.",
    },
    theme: COWORK_REGION_THEME,
  },
  {
    regionId: COWORK_REGION_IDS.healthStrip,
    role: COWORK_ROLE_IDS.healthStrip,
    presence: "required",
    help: {
      summary: "See the document name, drift state, and open-proposal count.",
      details:
        "The header health strip is read-only chrome. It reflects the document identity, whether the file has drifted from its last materialized form, and how many proposals are open.",
    },
    theme: COWORK_REGION_THEME,
  },
];

/**
 * The `wb.cowork.workspace` single-surface view (section 5). Grid slot and order fields
 * are empty by contract: the App renderer composes the three regions itself inside one
 * shared React tree rather than placing them on the widget grid.
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
  layoutKind: "single-surface",
  grid: { columns: 24 },
  defaultSlots: [],
  readingOrder: [],
  mobileOrder: [],
  surface: { regions: COWORK_REGIONS },
} as const satisfies ViewDefinition;
