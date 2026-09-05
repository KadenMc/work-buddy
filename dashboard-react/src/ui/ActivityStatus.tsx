import { Spinner } from "./Spinner";
import { Stack, Text } from "./Typography";

export interface ActivityStatusProps {
  /** The message describing the work in progress. */
  readonly label: string;
  /** A secondary line carrying a running quantity, for work with no known total. */
  readonly detail?: string;
}

/**
 * Busy state that can carry a running quantity.
 *
 * Indeterminate by construction: the caller reports how much work it has done, not how
 * much is left, so this is a status message rather than `role="progressbar"`.
 *
 * `role="status"` sits on the label alone, and that role is a polite, atomic live region:
 * anything inside it re-announces in full whenever it changes. The label is stable, so it
 * announces once. The detail ticks upward through a long operation, so it renders as a
 * sibling outside the region and carries `aria-hidden`, which keeps it a sighted-user
 * reassurance signal instead of a stream of announcements. The spinner mark is decorative
 * and hidden for the same reason. Busy state for the surrounding region is the caller's
 * `aria-busy`.
 */
export function ActivityStatus({ label, detail }: ActivityStatusProps) {
  return (
    <div className="wb-activity-status">
      <span className="wb-activity-status__indicator" aria-hidden="true">
        <Spinner label="" />
      </span>
      <Stack className="wb-activity-status__copy">
        <Text role="status">{label}</Text>
        {detail ? (
          <Text size="small" tone="muted" aria-hidden="true">
            {detail}
          </Text>
        ) : null}
      </Stack>
    </div>
  );
}
