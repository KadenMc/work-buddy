/**
 * The Co-work writing-flow slash menu (PRD section 7). The editor bundle spreads
 * CoworkSlashMenu into buildEditorExtensions, and the registry plus the popup component are
 * exported for the tests and any future insert affordance.
 */

export {
  CoworkSlashMenu,
  SLASH_MENU_PLUGIN_KEY,
  buildSlashSuggestionConfig,
  type CoworkSlashMenuOptions,
} from "./slashMenuExtension";
export { SlashMenu, type SlashMenuProps } from "./SlashMenu";
export {
  SLASH_COMMANDS,
  FORBIDDEN_INSERT_TYPES,
  SLASH_GROUP_LABEL,
  SLASH_GROUP_ORDER,
  filterSlashCommands,
  groupSlashCommands,
  moveActiveIndex,
  type SlashCommand,
  type SlashCommandGroup,
  type SlashRange,
} from "./slashCommands";
