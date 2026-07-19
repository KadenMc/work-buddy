import { useMemo } from "react";
import { Command } from "cmdk";

import {
  groupSlashCommands,
  SLASH_GROUP_LABEL,
  type SlashCommand,
} from "./slashCommands";
import "./styles.css";

/**
 * The slash menu popup body (PRD section 7). A controlled, presentational cmdk list: the
 * slash extension owns the query, the item set, and the active item, so this component only
 * renders and reports selection. The editor keeps DOM focus, so keyboard navigation is
 * driven from the extension through the `activeId` prop rather than cmdk's own focus loop,
 * and a pointer click routes through `onSelect`.
 */
export interface SlashMenuProps {
  readonly commands: readonly SlashCommand[];
  /** The highlighted command id, kept in step with the extension's keyboard cursor. */
  readonly activeId: string | null;
  onSelect(command: SlashCommand): void;
  /** Fired when a pointer hover moves the active item, so the extension stays in sync. */
  onActiveChange?(id: string): void;
}

export function SlashMenu({
  commands,
  activeId,
  onSelect,
  onActiveChange,
}: SlashMenuProps) {
  const sections = useMemo(() => groupSlashCommands(commands), [commands]);
  const byId = useMemo(
    () => new Map(commands.map((command) => [command.id, command])),
    [commands],
  );

  return (
    <Command
      className="wb-cowork-slash"
      label="Insert block"
      shouldFilter={false}
      value={activeId ?? undefined}
      onValueChange={onActiveChange}
    >
      <Command.List className="wb-cowork-slash__list" label="Insert block">
        {commands.length === 0 ? (
          <Command.Empty className="wb-cowork-slash__empty">
            No blocks match
          </Command.Empty>
        ) : (
          sections.map((section) => (
            <Command.Group
              key={section.group}
              heading={SLASH_GROUP_LABEL[section.group]}
              className="wb-cowork-slash__group"
            >
              {section.commands.map((command) => (
                <Command.Item
                  key={command.id}
                  value={command.id}
                  className="wb-cowork-slash__item"
                  onSelect={(value) => {
                    const picked = byId.get(value);
                    if (picked !== undefined) onSelect(picked);
                  }}
                >
                  <span className="wb-cowork-slash__item-title">
                    {command.title}
                  </span>
                  <span className="wb-cowork-slash__item-hint">
                    {command.hint}
                  </span>
                </Command.Item>
              ))}
            </Command.Group>
          ))
        )}
      </Command.List>
    </Command>
  );
}

export default SlashMenu;
