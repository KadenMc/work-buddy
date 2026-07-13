import { FingerprintSimple } from "@phosphor-icons/react/FingerprintSimple";

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
    <span className="wb-library-provenance" title={`Source: ${details}`}>
      <FingerprintSimple weight="duotone" aria-hidden="true" />
      {details}
    </span>
  );
}
