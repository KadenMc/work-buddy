import { VisuallyHidden } from "./VisuallyHidden";

export function Spinner({ label = "Loading" }: { readonly label?: string }) {
  return (
    <span role="status">
      <span className="wb-spinner" aria-hidden="true" />
      <VisuallyHidden>{label}</VisuallyHidden>
    </span>
  );
}
