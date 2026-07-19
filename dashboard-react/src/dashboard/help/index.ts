export { DashboardHelpProvider, HelpTarget, useDashboardHelpEnabled } from "./DashboardHelp";
export {
  HelpModeProvider,
  useHelpMode,
  type HelpModeController,
} from "./HelpModeController";
// HelpModeToggle is intentionally NOT re-exported here. It pulls the ui Button and the
// announcer, and this barrel is imported by lazily-loaded view chunks that only need the
// light HelpTarget / provider pieces. Keeping the heavy toggle out of the barrel avoids a
// cross-chunk cycle through the ui barrel. Import it directly from ./HelpModeToggle.
export type { HelpContent } from "./contracts";
