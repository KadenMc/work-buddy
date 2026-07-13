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
    <span className={`wb-library-badge wb-library-badge--${tone}`}>{label}</span>
  );
}
