import { useCallback, useState } from "react";
import { useSearchParams } from "react-router-dom";

import type {
  IntentResult,
  WidgetIntent,
} from "../../dashboard/contributions/contracts";
import { WidgetHost } from "../../dashboard/widgets/WidgetHost";
import type { ThemeSchemePreference } from "../../theme/contracts";
import { useTheme } from "../../theme/ThemeProvider";
import { listThemeSkins } from "../../theme/packs/registry";
import { SelectField } from "../../ui";
import {
  buildModeCases,
  buildStateCases,
  buildSyntheticTraceCases,
  WIDGET_LAB_DIMENSIONS,
  WIDGET_LAB_VIEW_ID,
  type WidgetLabCase,
} from "./labCases";
import "./styles.css";

const MAX_SYNTHETIC_WIDGETS = 200;

function parseSyntheticCount(value: string | null): number | null {
  if (value === null) return null;
  const count = Number(value);
  if (!Number.isInteger(count) || count < 1) return null;
  return Math.min(count, MAX_SYNTHETIC_WIDGETS);
}

function LabWidgetCase({
  labCase,
  onIntent,
}: {
  readonly labCase: WidgetLabCase;
  readonly onIntent: (intent: WidgetIntent) => Promise<IntentResult>;
}) {
  const dimensions = WIDGET_LAB_DIMENSIONS[labCase.sizeMode];
  return (
    <article
      className={`wb-widget-lab__case is-${labCase.sizeMode}`}
      data-testid="widget-lab-host"
      data-widget-type={labCase.widget.definition.typeId}
      data-size-mode={labCase.sizeMode}
      data-host-state={labCase.status}
    >
      <p className="wb-widget-lab__case-label">
        <code>{labCase.widget.definition.typeId}</code>
        <span>{labCase.sizeMode}</span>
        <span>{labCase.status}</span>
      </p>
      <WidgetHost
        definition={labCase.widget.definition}
        module={labCase.widget.module}
        instanceId={labCase.instanceId}
        viewId={WIDGET_LAB_VIEW_ID}
        input={labCase.input}
        status={labCase.status}
        statusMessage={`Widget Lab ${labCase.status} fixture.`}
        width={dimensions.width}
        height={dimensions.height}
        sizeMode={labCase.sizeMode}
        editing={false}
        emit={onIntent}
      />
    </article>
  );
}

function LabControls({ traceCount }: { readonly traceCount: number | null }) {
  const { theme, setPreference } = useTheme();
  return (
    <section className="wb-widget-lab__controls" aria-label="Widget Lab controls">
      <SelectField<ThemeSchemePreference>
        label="Widget Lab scheme"
        value={theme.preference.scheme}
        options={[
          { value: "system", label: "System" },
          { value: "light", label: "Light" },
          { value: "dark", label: "Dark" },
        ]}
        onChange={(scheme) => setPreference({ scheme })}
      />
      <SelectField
        label="Widget Lab skin"
        value={theme.preference.skinId}
        options={listThemeSkins().map((skin) => ({
          value: skin.identity.id,
          label: skin.label,
          description: skin.description,
        }))}
        onChange={(skinId) => setPreference({ skinId })}
      />
      <dl className="wb-widget-lab__environment">
        <div>
          <dt>Forced colors</dt>
          <dd data-testid="widget-lab-forced-colors">
            {theme.accessibility.forcedColors ? "active" : "inactive"}
          </dd>
        </div>
        <div>
          <dt>Reduced motion</dt>
          <dd data-testid="widget-lab-reduced-motion">
            {theme.accessibility.reducedMotion ? "active" : "inactive"}
          </dd>
        </div>
      </dl>
      {traceCount !== null && (
        <p className="wb-widget-lab__trace-label" role="status">
          Synthetic trace: exactly {traceCount} real widget hosts
        </p>
      )}
    </section>
  );
}

export default function WidgetLab() {
  const [searchParams] = useSearchParams();
  const traceCount = parseSyntheticCount(searchParams.get("count"));
  const [lastIntent, setLastIntent] = useState("No intent emitted");
  const recordIntent = useCallback(
    async (intent: WidgetIntent): Promise<IntentResult> => {
      setLastIntent(intent.intent_type);
      return { intent_id: intent.intent_id, status: "accepted" };
    },
    [],
  );

  const traceCases =
    traceCount === null ? null : buildSyntheticTraceCases(traceCount);

  return (
    <main className="wb-widget-lab">
      <header className="wb-widget-lab__header">
        <div>
          <p className="wb-widget-lab__eyebrow">Development only</p>
          <h1>Widget Lab</h1>
          <p>
            Registered renderers, real Journal fixture inputs, and the public host/theme
            contracts—without a parallel preview implementation.
          </p>
        </div>
        <output aria-live="polite">Last intent: {lastIntent}</output>
      </header>

      <LabControls traceCount={traceCount} />

      {traceCases === null ? (
        <>
          <section aria-labelledby="widget-lab-modes">
            <h2 id="widget-lab-modes">Renderer size modes</h2>
            <p>Every registered reusable widget at compact, standard, and expanded.</p>
            <div className="wb-widget-lab__grid">
              {buildModeCases().map((labCase) => (
                <LabWidgetCase
                  key={labCase.caseId}
                  labCase={labCase}
                  onIntent={recordIntent}
                />
              ))}
            </div>
          </section>

          <section aria-labelledby="widget-lab-states">
            <h2 id="widget-lab-states">Shared host states</h2>
            <p>
              Every registered reusable widget through ready, loading, empty, stale,
              offline, unavailable, permission-denied, error, and read-only host
              behavior.
            </p>
            <div className="wb-widget-lab__grid">
              {buildStateCases().map((labCase) => (
                <LabWidgetCase
                  key={labCase.caseId}
                  labCase={labCase}
                  onIntent={recordIntent}
                />
              ))}
            </div>
          </section>
        </>
      ) : (
        <section aria-labelledby="widget-lab-trace">
          <h2 id="widget-lab-trace">Synthetic widget trace</h2>
          <p>
            Cycles the real registered widgets and size modes; it does not replace them
            with test doubles.
          </p>
          <div className="wb-widget-lab__grid is-trace">
            {traceCases.map((labCase) => (
              <LabWidgetCase
                key={labCase.caseId}
                labCase={labCase}
                onIntent={recordIntent}
              />
            ))}
          </div>
        </section>
      )}
    </main>
  );
}
