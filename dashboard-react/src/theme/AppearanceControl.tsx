import { Desktop } from "@phosphor-icons/react/Desktop";
import { Moon } from "@phosphor-icons/react/Moon";
import { PaintBrushBroad } from "@phosphor-icons/react/PaintBrushBroad";
import { Sun } from "@phosphor-icons/react/Sun";
import { X } from "@phosphor-icons/react/X";
import { Dialog, DialogTrigger, Heading, Popover } from "react-aria-components";

import { IconButton, SelectField } from "../ui";
import { useDensity, type DashboardDensity } from "./DensityProvider";
import type { ThemeSchemePreference } from "./contracts";
import { listThemeSkins } from "./packs/registry";
import { useTheme } from "./ThemeProvider";

const schemeOptions: readonly {
  readonly value: ThemeSchemePreference;
  readonly label: string;
}[] = [
  { value: "system", label: "Follow this device" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

const densityOptions: readonly {
  readonly value: DashboardDensity;
  readonly label: string;
  readonly description: string;
}[] = [
  { value: "compact", label: "Compact", description: "More information at once" },
  { value: "comfortable", label: "Comfortable", description: "Balanced spacing" },
  { value: "spacious", label: "Spacious", description: "More room around controls" },
];

export function AppearanceControl() {
  const { theme, setPreference } = useTheme();
  const { density, setDensity } = useDensity();
  const schemeIcon =
    theme.preference.scheme === "system" ? (
      <Desktop weight="duotone" />
    ) : theme.preference.scheme === "dark" ? (
      <Moon weight="duotone" />
    ) : (
      <Sun weight="duotone" />
    );
  const skinOptions = listThemeSkins()
    .filter((skin) => skin.purpose === "product")
    .map((skin) => ({
      value: skin.identity.id,
      label: skin.label,
      description: skin.description,
    }));

  return (
    <DialogTrigger>
      <IconButton
        label="Appearance"
        icon={schemeIcon}
        variant="ghost"
        size="small"
        className="wb-appearance-trigger"
      />
      <Popover className="wb-popover wb-appearance-popover" placement="bottom end">
        <Dialog className="wb-appearance-dialog">
          {({ close }) => (
            <>
              <header className="wb-appearance-dialog__header">
                <div>
                  <span className="wb-appearance-dialog__eyebrow">
                    <PaintBrushBroad weight="duotone" aria-hidden="true" />
                    Dashboard
                  </span>
                  <Heading slot="title">Appearance</Heading>
                </div>
                <IconButton
                  label="Close appearance"
                  icon={<X weight="bold" />}
                  variant="ghost"
                  size="small"
                  onClick={close}
                />
              </header>
              <div className="wb-appearance-dialog__fields">
                <SelectField
                  label="Color scheme"
                  value={theme.preference.scheme}
                  options={schemeOptions}
                  onChange={(scheme) => setPreference({ scheme })}
                />
                <SelectField
                  label="Skin"
                  value={theme.preference.skinId}
                  options={skinOptions}
                  onChange={(skinId) => setPreference({ skinId })}
                />
                <SelectField
                  label="Density"
                  value={density}
                  options={densityOptions}
                  onChange={setDensity}
                />
              </div>
              <p className="wb-appearance-dialog__footnote">
                Apps and widgets inherit these semantic choices automatically.
              </p>
            </>
          )}
        </Dialog>
      </Popover>
    </DialogTrigger>
  );
}
