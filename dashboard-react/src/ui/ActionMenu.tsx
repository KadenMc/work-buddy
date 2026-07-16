import { DotsThree } from "@phosphor-icons/react/DotsThree";
import type { ReactNode } from "react";
import {
  Button as AriaButton,
  Header,
  Menu,
  MenuItem,
  MenuSection,
  MenuTrigger,
  Popover,
  type Key,
} from "react-aria-components";

export interface ActionMenuItemDefinition {
  readonly id: string;
  readonly label: string;
  readonly icon?: ReactNode;
  readonly disabled?: boolean;
  readonly tone?: "default" | "danger";
  onAction(): void;
}

export interface ActionMenuSectionDefinition {
  readonly id: string;
  readonly label?: string;
  readonly items: readonly ActionMenuItemDefinition[];
}

export function ActionMenu({
  label,
  sections,
  note,
}: {
  readonly label: string;
  readonly sections: readonly ActionMenuSectionDefinition[];
  readonly note?: ReactNode;
}) {
  const items = new Map(
    sections.flatMap((section) => section.items.map((item) => [item.id, item] as const)),
  );
  return (
    <MenuTrigger>
      <AriaButton className="wb-menu-trigger" aria-label={label}>
        <DotsThree weight="bold" aria-hidden="true" />
      </AriaButton>
      <Popover className="wb-popover wb-action-menu__popover" placement="bottom end">
        <Menu
          className="wb-action-menu"
          aria-label={label}
          onAction={(key: Key) => items.get(String(key))?.onAction()}
        >
          {sections.map((section) => (
            <MenuSection key={section.id} className="wb-action-menu__section">
              {section.label ? <Header>{section.label}</Header> : null}
              {section.items.map((item) => (
                <MenuItem
                  key={item.id}
                  id={item.id}
                  isDisabled={item.disabled}
                  textValue={item.label}
                  className={`wb-action-menu__item${item.tone === "danger" ? " is-danger" : ""}`}
                >
                  {item.icon ? (
                    <span className="wb-action-menu__icon" aria-hidden="true">
                      {item.icon}
                    </span>
                  ) : null}
                  <span>{item.label}</span>
                </MenuItem>
              ))}
            </MenuSection>
          ))}
        </Menu>
        {note ? <div className="wb-action-menu__note">{note}</div> : null}
      </Popover>
    </MenuTrigger>
  );
}
