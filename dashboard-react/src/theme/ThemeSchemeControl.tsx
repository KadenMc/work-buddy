import type { ThemeSchemePreference } from "./contracts";
import { useTheme } from "./ThemeProvider";

const schemeOptions: readonly {
  value: ThemeSchemePreference;
  label: string;
}[] = [
  { value: "system", label: "System theme" },
  { value: "light", label: "Light theme" },
  { value: "dark", label: "Dark theme" },
];

/** Global scheme preference. Skins remain a separate validated-pack axis. */
export function ThemeSchemeControl() {
  const { theme, setPreference } = useTheme();

  return (
    <label className="wb-theme-scheme-control">
      <span className="wb-visually-hidden">Color scheme</span>
      <select
        aria-label="Color scheme"
        value={theme.preference.scheme}
        onChange={(event) =>
          setPreference({
            scheme: event.currentTarget.value as ThemeSchemePreference,
          })
        }
      >
        {schemeOptions.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
