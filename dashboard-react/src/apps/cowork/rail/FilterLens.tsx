/**
 * The filter lens (SP-6 variant B as a lens over the stream). Single-select
 * typed-group chips with counts, dimming or hiding non-matching cards in place
 * rather than switching to a second layout. The type is named in text, so the
 * decorative colour swatch is not the only signal (SP-6 G3).
 */

import type { RailFilter } from "./store";

export interface FilterCounts {
  readonly all: number;
  readonly suggestions: number;
  readonly flags: number;
  readonly claims: number;
}

export interface FilterLensProps {
  readonly filter: RailFilter;
  readonly counts: FilterCounts;
  onChange(filter: RailFilter): void;
}

interface ChipSpec {
  readonly value: RailFilter;
  readonly label: string;
  readonly count: (counts: FilterCounts) => number;
  /** Data series token for the decorative swatch. */
  readonly series?: string;
}

const CHIPS: readonly ChipSpec[] = [
  { value: "all", label: "All", count: (counts) => counts.all },
  {
    value: "suggestions",
    label: "Suggestions",
    count: (counts) => counts.suggestions,
    series: "suggestions",
  },
  {
    value: "flags",
    label: "Flags",
    count: (counts) => counts.flags,
    series: "flags",
  },
  {
    value: "claims",
    label: "Claims",
    count: (counts) => counts.claims,
    series: "claims",
  },
];

export function FilterLens({ filter, counts, onChange }: FilterLensProps) {
  return (
    <div
      className="wb-cowork-rail__filters"
      role="group"
      aria-label="Filter review items by type"
    >
      {CHIPS.map((chip) => {
        const active = filter === chip.value;
        return (
          <button
            key={chip.value}
            type="button"
            className="wb-cowork-rail__chip"
            data-series={chip.series}
            aria-pressed={active}
            onClick={() => onChange(chip.value)}
          >
            {chip.series !== undefined ? (
              <span
                className="wb-cowork-rail__chip-swatch"
                data-series={chip.series}
                aria-hidden="true"
              />
            ) : null}
            <span className="wb-cowork-rail__chip-label">{chip.label}</span>
            <span className="wb-cowork-rail__chip-count">
              {chip.count(counts)}
            </span>
          </button>
        );
      })}
    </div>
  );
}
