import { useCallback, useMemo, useRef, useState } from "react";

import { asWidgetInstanceId } from "../../dashboard/contributions/contracts";
import { ReactGridLayoutAdapter } from "../../dashboard/layout/ReactGridLayoutAdapter";
import type { DashboardLayout } from "../../dashboard/layout/contracts";
import { WidgetFrame } from "../../dashboard/widgets/WidgetFrame";
import type { ThemeSchemePreference } from "../../theme/contracts";
import { listThemeSkins } from "../../theme/packs/registry";
import { useTheme } from "../../theme/ThemeProvider";
import { Button, SegmentedControl, SelectField } from "../../ui";
import type {
  CalendarSurfaceIntent,
  CalendarSurfaceIntentResult,
  CalendarSurfaceModel,
  CalendarSurfacePresentation,
  CalendarSurfaceRange,
} from "../../widget-library/timeline/calendar-surface/contracts";
import { CalendarSurface } from "../../widget-library/timeline/calendar-surface/CalendarSurface";
import {
  CALENDAR_SPIKE_FIXTURE_LIST,
  CALENDAR_SPIKE_FIXTURES,
  type CalendarSpikeFixtureId,
} from "./fixtures";
import "./styles.css";

const CALENDAR_INSTANCE_ID = asWidgetInstanceId("calendar-spike:surface");
const NARROW_GRID_WIDTH = 10;

const initialLayout: DashboardLayout = [
  {
    instanceId: CALENDAR_INSTANCE_ID,
    x: 0,
    y: 0,
    w: 20,
    h: 14,
    minW: 8,
    maxW: 24,
    minH: 8,
    maxH: 22,
  },
];

type MutationResponse = "accept" | "reject" | "conflict";

const responseResult = (
  response: MutationResponse,
  revision: string,
): CalendarSurfaceIntentResult => {
  if (response === "accept") return { status: "accepted", revision };
  if (response === "conflict") {
    return {
      status: "conflict",
      message: "The fixture revision changed; the calendar move was restored.",
    };
  }
  return {
    status: "rejected",
    message: "The fixture provider rejected the change; the calendar move was restored.",
  };
};

const nextRevision = (current: string, sequence: number) => `${current}:spike-${sequence}`;

export default function CalendarSpike() {
  const { theme, setPreference } = useTheme();
  const revisionSequence = useRef(0);
  const [fixtureId, setFixtureId] = useState<CalendarSpikeFixtureId>("july11");
  const [model, setModel] = useState<CalendarSurfaceModel>(CALENDAR_SPIKE_FIXTURES.july11);
  const [layout, setLayout] = useState<DashboardLayout>(initialLayout);
  const [mutationResponse, setMutationResponse] = useState<MutationResponse>("accept");
  const [lastIntent, setLastIntent] = useState("No intent emitted");
  const [intentCount, setIntentCount] = useState(0);
  const [openCount, setOpenCount] = useState(0);
  const [announcement, setAnnouncement] = useState("No rollback announcement");

  const selectFixture = (nextFixtureId: CalendarSpikeFixtureId) => {
    setFixtureId(nextFixtureId);
    setModel(CALENDAR_SPIKE_FIXTURES[nextFixtureId]);
    setLastIntent("No intent emitted");
    setIntentCount(0);
    setOpenCount(0);
    setAnnouncement("No rollback announcement");
  };

  const updateRange = (range: CalendarSurfaceRange) => {
    setModel((current) => ({ ...current, view: { ...current.view, range } }));
  };

  const updatePresentation = (presentation: CalendarSurfacePresentation) => {
    setModel((current) => ({ ...current, view: { ...current.view, presentation } }));
  };

  const handleIntent = useCallback(
    (intent: CalendarSurfaceIntent): CalendarSurfaceIntentResult => {
      setLastIntent(intent.type);
      setIntentCount((count) => count + 1);

      if (intent.type === "calendar.range-requested") {
        return { status: "accepted", revision: model.revision };
      }
      if (intent.type === "calendar.item-open-requested") {
        setOpenCount((count) => count + 1);
        return { status: "accepted", revision: model.revision };
      }

      revisionSequence.current += 1;
      const revision = nextRevision(model.revision, revisionSequence.current);
      const result = responseResult(mutationResponse, revision);
      if (result.status !== "accepted") return result;

      if (intent.type === "calendar.item-action-requested") return result;

      if (intent.type === "calendar.item-create-requested") {
        setModel((current) => ({
          ...current,
          revision,
          items: [
            ...current.items,
            {
              id: `fixture-created-${revisionSequence.current}`,
              revision: `${revision}:item`,
              sourceId: intent.sourceId ?? current.sources[0]?.sourceId ?? "fixture",
              placement: intent.placement,
              kind: "plan",
              title: "Fixture-created calendar item",
              detail: "Created through the Work Buddy intent boundary",
              status: "planned",
              provenance: { source: "user", label: "you" },
              capabilities: { open: true, move: true, resize: true, remove: true },
              appearance: { tone: "data-2", emphasis: "normal" },
            },
          ],
        }));
        return result;
      }

      const item = model.items.find((candidate) => candidate.id === intent.itemId);
      if (!item || item.revision !== intent.expectedRevision) {
        return {
          status: "conflict",
          message: "The fixture item revision was stale; the change was restored.",
        };
      }

      setModel((current) => ({
        ...current,
        revision,
        items:
          intent.type === "calendar.item-remove-requested"
            ? current.items.filter((candidate) => candidate.id !== intent.itemId)
            : current.items.map((candidate) =>
                candidate.id === intent.itemId
                  ? {
                      ...candidate,
                      revision: `${revision}:item`,
                      placement: intent.placement,
                    }
                  : candidate,
              ),
      }));
      return result;
    },
    [model, mutationResponse],
  );

  const selectedFixture = useMemo(
    () => CALENDAR_SPIKE_FIXTURE_LIST.find((fixture) => fixture.fixtureId === fixtureId)!,
    [fixtureId],
  );

  return (
    <main className="wb-calendar-spike">
      <header className="wb-calendar-spike__header">
        <div>
          <p className="wb-calendar-spike__eyebrow">Development-only acceptance spike</p>
          <h1>FullCalendar surface spike</h1>
          <p>
            FullCalendar 6.1.21 behind a library-neutral Work Buddy adapter, fixture
            provider, semantic skin bridge, and resizable grid host.
          </p>
        </div>
        <dl className="wb-calendar-spike__telemetry" aria-label="Spike telemetry">
          <div><dt>Last intent</dt><dd data-testid="calendar-spike-last-intent">{lastIntent}</dd></div>
          <div><dt>Intent count</dt><dd data-testid="calendar-spike-intent-count">{intentCount}</dd></div>
          <div><dt>Open count</dt><dd data-testid="calendar-spike-open-count">{openCount}</dd></div>
        </dl>
      </header>

      <section className="wb-calendar-spike__controls" aria-label="Calendar spike controls">
        <SelectField
          label="Fixture"
          value={fixtureId}
          options={CALENDAR_SPIKE_FIXTURE_LIST.map((fixture) => ({
            value: fixture.fixtureId,
            label: fixture.label,
            description: fixture.description,
          }))}
          onChange={selectFixture}
        />
        <SelectField<ThemeSchemePreference>
          label="Scheme"
          value={theme.preference.scheme}
          options={[
            { value: "system", label: "System" },
            { value: "light", label: "Light" },
            { value: "dark", label: "Dark" },
          ]}
          onChange={(scheme) => setPreference({ scheme })}
        />
        <SelectField
          label="Skin"
          value={theme.preference.skinId}
          options={listThemeSkins().map((skin) => ({
            value: skin.identity.id,
            label: skin.label,
            description: skin.description,
          }))}
          onChange={(skinId) => setPreference({ skinId })}
        />
        <SelectField<MutationResponse>
          label="Mutation response"
          value={mutationResponse}
          options={[
            { value: "accept", label: "Accept" },
            { value: "reject", label: "Reject and revert" },
            { value: "conflict", label: "Conflict and revert" },
          ]}
          onChange={setMutationResponse}
        />
        <Button size="small" onClick={() => selectFixture(fixtureId)}>Reset fixture</Button>
      </section>

      <section className="wb-calendar-spike__fixture-note" aria-label="Selected fixture">
        <strong>{selectedFixture.label}</strong>
        <span>{selectedFixture.description}</span>
        <span>{model.items.length} items · {model.timezone}</span>
        {model.access.mode === "read_only" ? <span>{model.access.reason}</span> : null}
      </section>

      <ReactGridLayoutAdapter
        items={layout}
        editMode
        rowHeight={32}
        margin={[12, 12]}
        onDraftChange={setLayout}
        renderItem={(item) => {
          return (
            <WidgetFrame title="Candidate calendar surface" className="wb-calendar-spike__frame">
              <div
                className="wb-calendar-spike__surface-body"
                data-wb-calendar-responsive-mode={model.view.presentation}
              >
                <div className="wb-calendar-spike__surface-toolbar">
                  <SegmentedControl<CalendarSurfaceRange>
                    label="Calendar range"
                    value={model.view.range}
                    options={[
                      { value: "day", label: "Day" },
                      { value: "week", label: "Week" },
                      { value: "month", label: "Month" },
                    ]}
                    onChange={updateRange}
                  />
                  <SegmentedControl<CalendarSurfacePresentation>
                    label="Calendar presentation"
                    value={model.view.presentation}
                    options={[
                      { value: "calendar", label: "Calendar" },
                      { value: "list", label: "List" },
                    ]}
                    onChange={updatePresentation}
                  />
                  <span>{item.w} × {item.h} grid units</span>
                </div>
                <CalendarSurface
                  model={model}
                  density={item.w <= NARROW_GRID_WIDTH ? "compact" : "comfortable"}
                  onIntent={handleIntent}
                  onAnnouncement={(message) => setAnnouncement(message)}
                />
              </div>
            </WidgetFrame>
          );
        }}
      />

      <p className="wb-calendar-spike__announcement" role="status" aria-live="assertive">
        {announcement}
      </p>
      <p className="wb-calendar-spike__boundary-note">
        Google credentials, provider SDKs, and FullCalendar types are intentionally absent
        from the model and intent boundary.
      </p>
    </main>
  );
}
