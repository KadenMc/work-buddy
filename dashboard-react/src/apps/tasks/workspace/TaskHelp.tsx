import { forwardRef } from "react";
import { HelpTarget, type HelpContent } from "../../../dashboard/help";
import { Button, type ButtonProps } from "../../../ui/Button";

const hint = (summary: string, details: string): HelpContent => ({ summary, details });

/** Effects and recovery belong beside the control, including in compact layouts. */
export const TASK_HELP = {
  complete: hint("Open a confirmation before completing this task.", "The checkmark does not change anything until you confirm. Completion removes the task from an Open-only list; include Completed in Status and choose Reopen to recover it."),
  reopen: hint("Reopen this completed task immediately.", "The task returns to the Open lifecycle. Complete it again if this was unintended; the task and its project links remain intact."),
  status: hint("Filter by lifecycle: Open, Completed, Archived, or Trash.", "Open includes snoozed tasks. Multiple statuses match any selected status; clearing them includes every lifecycle. Attention is a separate way to narrow work within these statuses. Filters update immediately."),
  attention: hint("Filter by how you are attending to the work.", "Inbox, Most Important, Active, Working on now, Waiting, and Snoozed describe attention. They do not replace lifecycle Status. Multiple attention values match any selection, combined with all other filters."),
  editAttention: hint("Choose how you are attending to this task.", "This changes the draft until you save. Lifecycle actions such as Complete, Archive, and Move to trash are separate. Snoozing uses the lifecycle controls and its chosen date."),
  projectFilter: hint("Find tasks linked to any selected registered project.", "A task can link to zero, one, or several projects independently of its namespace assignments. No project finds tasks without a link; unresolved historical links are listed separately."),
  projectLinks: hint("Link this task to zero, one, or several projects.", "Choose registered projects here. These edits are part of the task draft and take effect when it is created or saved. Project links do not create, move, or remove namespaces."),
  namespaceField: hint("Assign independent namespaces to this task.", "Enter comma-separated paths; slashes create hierarchy. These are draft edits until the task is created or saved. Project links are separate; use Manage namespaces for changes across tasks."),
  namespaceFilter: hint("Include this namespace and all its descendants.", "Selection only filters the list. Use the scope control to match direct assignments only. Selecting several namespaces matches any of them, combined with your other filters."),
  namespaceExact: hint("Switch between direct assignments and the whole branch.", "Including descendants matches this path and deeper paths. This namespace only matches direct assignments to this exact path. The change updates the list immediately and does not reorganize anything."),
  noNamespace: hint("Show tasks with no namespace assignments.", "This filters the list without changing task records. Tasks can still have project links even when they have no namespace."),
  sort: hint("Choose which task field orders these results.", "Dates default to newest first, except due dates start with the earliest. Titles start A–Z and urgency starts highest first. The adjacent arrow reverses direction. Unknown dates remain last."),
  sortDirection: hint("Reverse the current sort direction.", "This changes the order of the matching results immediately. It does not change their filters or any task fields."),
  namespaceSelection: hint("Select namespaces to reorganize.", "Selection does not modify tasks. Choose an operation, review its destinations and affected tasks across every lifecycle, then apply it. Direct counts and descendant counts describe different assignment scopes."),
  preview: hint("Preview this namespace change without writing it.", "Review the path mapping, collisions, affected tasks, and lifecycle counts. Applying the reviewed preview is the separate step that updates assignments."),
  apply: hint("Apply the reviewed namespace change to all affected tasks.", "This writes the assignments shown in the preview, including completed, archived, and trashed tasks when the scope is all lifecycle records. A stale preview must be refreshed. Undo is available when the affected assignments can still be restored safely."),
  undoNamespaces: hint("Undo this recorded namespace operation.", "This restores the affected assignments if subsequent edits have not made that unsafe. A conflict leaves the current assignments intact. Project links and task identities are unchanged."),
  archive: hint("Archive this task immediately.", "It leaves an Open-only list. Include Archived in Status to find it and use Unarchive to restore its previous lifecycle. Its task record, project links, and document remain available."),
  unarchive: hint("Remove this task from the archive.", "The previous completion state is preserved: an already completed task stays completed. Use the Status filter to find the restored task."),
  trash: hint("Review moving this task to Trash.", "The next confirmation moves the task out of ordinary browsing. Its task record and knowledge document remain recoverable with Undo or Restore from Trash."),
  confirmTrash: hint("Move this task to Trash now.", "The task and knowledge document remain recoverable. Use Undo delete immediately, or include Trash in Status and restore the task later."),
  restore: hint("Restore this task from Trash.", "The task becomes available again with its previous archive and completion state. Change the Status filter if it still does not appear in an Open-only list."),
  snooze: hint("Snooze this task until the chosen date.", "This changes Attention to Snoozed immediately. It remains an Open task and stays visible when Open is the only lifecycle filter. Choose another attention value to resume it earlier."),
  focus: hint("Mark this task as Working on now.", "This immediately changes its attention state. It may leave a filtered Inbox or other attention list; clear the Attention filter to find it again."),
  createFromProposal: hint("Create the task from this reviewed proposal.", "Accept the saved fields and any additional proposed settings shown here. New namespaces require a separate structure confirmation. Retrying this same proposal resolves to the same task, not a second copy."),
} as const;

const BUTTON_HELP: Readonly<Record<string, HelpContent>> = {
  Complete: TASK_HELP.complete, Reopen: TASK_HELP.reopen,
  Archive: TASK_HELP.archive, Unarchive: TASK_HELP.unarchive, Restore: TASK_HELP.restore,
  "Move to trash": TASK_HELP.trash, "Undo delete": TASK_HELP.restore,
  "Working on now": TASK_HELP.focus, Snooze: TASK_HELP.snooze,
  "Most Important this week": hint("Mark this task Most Important immediately.", "This changes its attention state to Most Important. It may leave the current Inbox triage results; use the Attention filter to find it."),
  "Skip this pass": hint("Move this task to the end of the current triage pass.", "Skipping changes only this browsing order. It does not complete, snooze, archive, or edit the task."),
  "Show namespaces": hint("Show the namespace browsing panel.", "The panel lets you filter by hierarchy and direct assignments. Showing it preserves every active filter."),
  "Hide namespaces": hint("Hide this panel and give its space back to the task list.", "Active namespace filters remain applied and visible as removable pills. Show namespaces restores the panel."),
  "Manage namespaces": hint("Open the namespace organizer.", "Choose existing namespaces, then rename, move, merge, promote children, or remove assignments. Every operation has a preview; your current task-list filters do not limit its all-lifecycle scope."),
  "Triage inbox": hint("Start a guided pass through open Inbox tasks.", "This visibly selects Open status and Inbox attention while preserving other filters. Each row offers immediate actions and a Skip option."),
  "Finish triage": hint("Return to ordinary task browsing.", "The visible filters stay applied. Clear or change Open and Inbox pills if you want to broaden the results."),
  "Clear filters": hint("Remove every filter, including lifecycle Status.", "All tasks become eligible, including Completed, Archived, and Trash. Sorting remains unchanged; no task records are edited."),
  Rename: hint("Rename the final segment of one namespace.", "Descendant suffixes are preserved. Choose a new name, then preview affected assignments and any existing destination before applying."),
  Move: hint("Move selected namespace branches beneath a new parent.", "Choose Root to move a branch to the top level. Descendant paths follow their branch. The preview shows collisions and all affected lifecycle records before a write."),
  Merge: hint("Merge selected namespace branches into an existing destination.", "Descendant suffixes are preserved and duplicate assignments become one. Tasks remain separate. Preview the mapping and affected records before applying."),
  "Move children up one level": hint("Promote this namespace’s children to its parent.", "Deeper suffixes are preserved. If tasks are assigned directly to this grouping, choose whether those assignments stay or move before previewing."),
  "Remove assignments": hint("Remove the selected namespace assignments from tasks.", "Task records, project links, and documents remain. Choose whether descendants are included, then review which tasks would be left without a namespace."),
  "Preview change": TASK_HELP.preview, "Refresh preview": TASK_HELP.preview,
  "Retry same change": hint("Check or retry the same reviewed operation.", "The exact request and operation identifier are reused after an uncertain response, preventing a duplicate application. Your proposal remains available if the retry fails."),
  "Inspect affected tasks": hint("Inspect the task records affected by this preview.", "The list shows before and after assignments. Paging through these records does not apply or alter the proposal."),
  "Save changes": hint("Save the fields currently shown in this task draft.", "A revision check protects changes made elsewhere. Unsaved edits remain on this device if saving fails. Lifecycle actions below are separate immediate operations."),
  "Back to tasks": hint("Return to the task list with your browsing position restored.", "Unsaved task edits are retained on this device. Reopening the task restores its draft; returning to the list does not save those fields to the task."),
  "Save proposal changes": hint("Save these reviewed fields to the proposal.", "This updates the saved proposal immediately without creating a task. Choose Create task separately when the saved proposal is ready."),
  "Create task": TASK_HELP.createFromProposal,
  "Retry creating task": hint("Retry task creation from this same proposal.", "The saved proposal resolves to the same task if creation already succeeded. Retrying does not create a second copy."),
  "Refresh proposal": hint("Read the latest state of this proposal.", "Refreshing does not create, revise, or dismiss it. Your local edits remain protected if the saved proposal changed."),
  "Dismiss proposal": hint("Review dismissing this proposal.", "Opening the confirmation changes nothing. Confirm dismissal records the decision without creating a task; the original capture is kept."),
  "Confirm dismissal": hint("Dismiss this proposal now.", "This records the dismissal immediately. No task is created, and the original capture is kept."),
  "Keep proposal": hint("Cancel this dismissal and keep reviewing.", "This closes the confirmation without changing the saved proposal or creating a task."),
  "Confirm structure and create task": hint("Create the task with the new structure shown above.", "This accepts the reviewed proposal and its namespace assignments. Retrying the same proposal resolves to the same task."),
  "Discard local edits and load current proposal": hint("Replace your local edits with the current saved proposal.", "This discards the unsaved proposal edits on this device. It does not change the saved proposal or create a task."),
  "Discard my edits and load saved task": hint("Replace your local draft with the saved task.", "This discards your unsaved edits on this device. The saved task is unchanged; review the comparison before discarding your draft."),
  "Keep my draft against this saved version": hint("Keep your draft for another review against the latest saved task.", "This does not save anything yet. Review the differences before Save changes, which will write your draft fields over the saved values shown here."),
  "Open in Co-work": hint("Open this task’s existing knowledge document in Co-work.", "This navigates to the linked document without creating another document or changing task fields. Unsaved task edits remain on this device."),
  "Create knowledge document": hint("Create and link a knowledge document for this task.", "This saves the new document link immediately, separately from the task-field draft. Open it in Co-work to write long-form context."),
  "Add action item": hint("Add this checklist item to the task immediately.", "This saves the item separately from the task-field draft. Remove it from the checklist if it was unintended; removed items can be restored."),
  Edit: hint("Edit this action item’s text.", "Opening the editor changes nothing. Save item writes the change immediately; Cancel keeps its saved text."),
  "Save item": hint("Save this action item’s text immediately.", "This writes only the checklist item, separately from the task-field draft. Edit the item again to revise its text."),
  "Move up": hint("Move this action item one position earlier.", "This saves the checklist order immediately. It does not change the task list’s sorting. Move down reverses the move."),
  "Move down": hint("Move this action item one position later.", "This saves the checklist order immediately. It does not change the task list’s sorting. Move up reverses the move."),
  "Complete item": hint("Complete this action item immediately.", "Only this checklist item changes; the task’s status stays the same. Choose Reopen item to reverse it."),
  "Reopen item": hint("Reopen this action item immediately.", "This does not change the lifecycle of the task that contains it."),
  "Make current": hint("Make this the task’s current action item.", "This updates the next-action pointer immediately. Other action items remain in the task."),
  Approve: hint("Record approval of this action item.", "This records approval immediately without completing the item or its task."),
  Remove: hint("Remove this action item from the active checklist.", "The removed item remains recoverable under deleted action items. Its containing task is unchanged."),
  "Restore item": hint("Restore this removed action item.", "It returns to the task’s checklist. This does not restore or reopen the containing task itself."),
};

export const TaskButton = forwardRef<HTMLButtonElement, ButtonProps & { readonly help?: HelpContent }>(function TaskButton({ help, children, ...props }, ref) {
  const content = help ?? (typeof children === "string" ? BUTTON_HELP[children] : undefined) ?? BUTTON_HELP[props["aria-label"] ?? ""];
  return <HelpTarget content={content} reactAriaComposite><Button {...props} title={props.title ?? content?.summary} ref={ref}>{children}</Button></HelpTarget>;
});

export { HelpTarget as TaskHelp };
