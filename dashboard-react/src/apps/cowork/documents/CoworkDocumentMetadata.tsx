import type { CoworkScratchSummary } from "../contracts";

const localDocumentActivity = (
  document: CoworkScratchSummary,
): string | null => {
  if (document.recoveredFromPreviousEditor) {
    return "Recovered from an earlier session";
  }
  const edited = document.updatedAt !== document.createdAt;
  const activityAt = new Date(edited ? document.updatedAt : document.createdAt);
  if (Number.isNaN(activityAt.getTime())) return null;
  return `${edited ? "Edited" : "Created"} ${new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(activityAt)}`;
};

export const coworkLocalDocumentMetadataText = (
  document: CoworkScratchSummary,
): string => {
  const activity = localDocumentActivity(document);
  return [
    "Not saved to folder",
    "Saved in this browser",
    ...(activity === null ? [] : [activity]),
  ].join(" · ");
};

export function CoworkLocalDocumentMetadata({
  document,
}: {
  readonly document: CoworkScratchSummary;
}) {
  const activity = localDocumentActivity(document);
  return (
    <>
      <em>Not saved to folder</em>
      {" · Saved in this browser"}
      {activity === null ? null : ` · ${activity}`}
    </>
  );
}

export const coworkFolderDocumentMetadata = (
  folderName: string,
  path: string,
): string => `${folderName} · ${path}`;
