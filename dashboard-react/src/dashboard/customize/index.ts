export {
  CustomizeModeProvider,
  useCustomizeMode,
  type CustomizeModeController,
  type CustomizeModeHandle,
  type CustomizeModeRegistration,
} from "./CustomizeModeController";
// CustomizeViewToggle is intentionally NOT re-exported here. It pulls the ui Button and the
// HelpTarget trigger, and this barrel is imported by shell and view code that only needs the
// light provider / hook / types. Keeping the heavy toggle out of the barrel avoids a
// cross-chunk cycle through the ui barrel. Import it directly from ./CustomizeViewToggle.
