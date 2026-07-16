import type { WidgetProvenance } from "./contracts";
import "./styles.css";

export function ProvenanceBadge({
  provenance,
}: {
  readonly provenance: WidgetProvenance;
}) {
  const details = provenance.actor
    ? `${provenance.label}, by ${provenance.actor}`
    : provenance.label;
  return (
    <span className="wb-library-badge" title={`Source: ${details}`}>
      {details}
    </span>
  );
}
