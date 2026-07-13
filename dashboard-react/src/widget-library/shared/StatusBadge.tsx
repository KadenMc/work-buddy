import "./styles.css";

export type StatusBadgeTone = "neutral" | "info" | "success" | "warning" | "danger";

export function StatusBadge({
  label,
  tone = "neutral",
}: {
  readonly label: string;
  readonly tone?: StatusBadgeTone;
}) {
  return (
    <span className={`wb-library-status wb-library-status--${tone}`}>
      {tone !== "neutral" ? <span className="wb-library-status__dot" aria-hidden="true" /> : null}
      {label}
    </span>
  );
}
