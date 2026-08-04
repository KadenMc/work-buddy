import { useId, useState } from "react";

import { Button } from "../ui";
import type { KeybindingCommandDefinition } from "./contracts";
import {
  formatShortcutChord,
  shortcutChordFromEvent,
  type KeybindingMap,
  type KeybindingValidationIssue,
} from "./keybindings";

export interface KeybindingMapEditorProps {
  readonly commands: readonly KeybindingCommandDefinition[];
  readonly value: KeybindingMap;
  readonly issues: readonly KeybindingValidationIssue[];
  readonly disabled?: boolean;
  onChange(value: KeybindingMap): void;
}

/** Reusable, inline keyboard-capture control for an atomic shortcut map. */
export function KeybindingMapEditor(props: KeybindingMapEditorProps) {
  const idPrefix = useId();
  const [capturing, setCapturing] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");

  return (
    <div className="wb-keybinding-map" aria-label="Keyboard shortcuts">
      <p className="wb-keybinding-map__instructions">
        Choose Rebind, then press the new shortcut. Press Escape to cancel.
      </p>
      <div className="wb-keybinding-map__rows">
        {props.commands.map((command) => {
          const binding = props.value[command.commandId] ?? "";
          const commandIssues = props.issues.filter(
            (issue) => issue.commandId === command.commandId,
          );
          const isCapturing = capturing === command.commandId;
          const errorId = `${idPrefix}-keybinding-error-${command.commandId}`;
          return (
            <div
              key={command.commandId}
              className="wb-keybinding-map__row"
              data-invalid={commandIssues.length > 0 || undefined}
            >
              <div className="wb-keybinding-map__command">
                <strong>{command.label}</strong>
                {command.description ? <span>{command.description}</span> : null}
              </div>
              <kbd className="wb-keybinding-map__key">
                {isCapturing ? "Press keys…" : formatShortcutChord(binding)}
              </kbd>
              <Button
                variant="secondary"
                size="small"
                disabled={props.disabled}
                aria-label={`Rebind ${command.label}`}
                aria-describedby={commandIssues.length > 0 ? errorId : undefined}
                onClick={() => {
                  setCapturing(command.commandId);
                  setAnnouncement(`Listening for a new ${command.label} shortcut.`);
                }}
                onKeyDown={(event) => {
                  if (!isCapturing) return;
                  if (event.key === "Escape") {
                    event.preventDefault();
                    event.stopPropagation();
                    setCapturing(null);
                    setAnnouncement(`Cancelled rebinding ${command.label}.`);
                    return;
                  }
                  if (event.key === "Tab") {
                    setCapturing(null);
                    setAnnouncement(`Cancelled rebinding ${command.label}.`);
                    return;
                  }
                  const chord = shortcutChordFromEvent(event.nativeEvent);
                  if (chord === null) return;
                  event.preventDefault();
                  event.stopPropagation();
                  props.onChange({
                    ...props.value,
                    [command.commandId]: chord,
                  });
                  setCapturing(null);
                  setAnnouncement(
                    `${command.label} is now ${formatShortcutChord(chord)}. Save to apply it.`,
                  );
                }}
                onBlur={() => {
                  if (!isCapturing) return;
                  setCapturing(null);
                  setAnnouncement(`Cancelled rebinding ${command.label}.`);
                }}
              >
                {isCapturing ? "Listening…" : "Rebind"}
              </Button>
              {commandIssues.length > 0 ? (
                <p id={errorId} className="wb-keybinding-map__error" role="alert">
                  {commandIssues.map((issue) => issue.message).join(" ")}
                </p>
              ) : null}
            </div>
          );
        })}
      </div>
      <p className="wb-visually-hidden" aria-live="polite">
        {announcement}
      </p>
    </div>
  );
}
