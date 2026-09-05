---
name: Co-work Folder Setup Boundary
kind: system
description: The read-only proof that a selected folder is the only Co-work folder on its line of descent, and the explicit all-or-nothing setup that writes only after that proof holds.
summary: Choosing a folder inspects it without mutation, classifying its own Work Buddy data, its ancestors, and a budgeted walk of its descendants. Setup is a separate call that re-walks the whole tree under the folder operation locks and writes only into a folder that still classifies as uninitialized. Every refusal carries a typed code naming both the folder's state and the action that answers it.
entry_points:
- work_buddy.cowork.project_store
- work_buddy.cowork.folder_api
tags:
- cowork
- folders
- setup
- inspection
- boundaries
- refusals
aliases:
- folder setup
- folder inspection
- nested folder boundary
- descendant scan
- setup refusal
- folder boundary proof
parents:
- cowork/folder
dev_notes: |
  The descendant walk is budgeted in work units, not files, because a file count
  prices two very different trees the same. Opening a directory costs two
  metadata lookups plus a listing, while reading one entry out of an open
  listing is served from the enumeration already in hand. Measured against a
  large repository the first is roughly forty times the second, so the walk
  charges a directory `_SCAN_DIRECTORY_WEIGHT` (40) and an entry 1. Both budgets
  are derived from that weight: `DEFAULT_SCAN_WORK_PER_PAGE` (20,000) affords
  five hundred directory opens per page, and `DEFAULT_SCAN_WORK_LIMIT` (750,000)
  affords eighteen thousand seven hundred and fifty before the folder is refused
  as too large. The weight describes the hardware rather than a caller's
  preference, and the predicate reads only the shape of the tree, so the same
  tree refuses on every machine.

  A directory is charged only once its metadata lookup and its listing have both
  succeeded. A directory that vanished between being queued and being read costs
  nothing rather than being billed for work the walk never did, and a path that
  does not exist cannot hold a store, so dropping it leaves the proof intact. A
  directory re-read after an mtime race is charged for each read, so the total
  is exact only for a quiescent tree.

  The two budgets are independent knobs and neither is derived from the other at
  construction. The page budget bounds one request; the cumulative limit bounds
  the whole scan and is carried across pages in the scan cursor. A walk that
  restarted its total at every page would spend unbounded work while no single
  page reached the threshold, and an arbitrarily large folder would classify as
  a setup candidate. A page budget larger than the threshold simply means one
  page reaches it, and flooring the threshold to the page budget would discard a
  deliberately small one, so neither is clamped to the other.

  The paged walk is an advisory preview. It drives the launcher and mints an
  inspection token, and it trusts the pages it already walked instead of
  revalidating them on each continuation. The proof that gates a write is
  `initialize`, which re-walks the whole tree in one unpaged pass under the
  folder operation locks and refuses anything that does not classify as
  `uninitialized`. A preview that drifted between pages therefore cannot
  authorize an unsafe setup: the locked re-walk sees the store that appeared and
  stops the write. Ownership and exact classification gate the walk, so a
  continuation token exists only where no ancestor owns the folder and the
  folder itself classified as `uninitialized`; a continuation carries no
  authority those two would have to re-establish.

  `inspection_fingerprint` is a digest over existence, mtime, and size for the
  folder root, `.wbuddy/manifest.yaml`, `.wbuddy/cowork/store.yaml`, and
  `.wbuddy/cowork/store.db`. It expresses one thing: a caller's earlier
  observation of the folder, held so setup can refuse a folder that moved since
  a human was shown it. Passing `None` means the caller has no such gap to
  honour, and the refusal then carries the classification's own code and prose,
  naming what the folder is rather than implying a race that did not happen. A
  caller that does pass one collapses every non-`uninitialized` classification
  into `folder_changed`, which is correct for a human-facing surface and wrong
  for a programmatic one. `None` is safe because neither caller rests its safety
  on the fingerprint: the walk inside the locks classifies from the filesystem
  and refuses anything that is not `uninitialized`, and the managed-layout
  assertion re-runs around every write beneath the folder.

  Membership in `_RETRYABLE_SETUP_REFUSALS` decides both the `retryable` flag
  and the HTTP status for a refusal that `_setup_refusal` composes, so those two
  cannot drift apart. Keep the set restricted to codes a `FolderInspection` can
  actually carry as a `reason_code`; a code that exists only as a raised
  exception makes the lookup unconditionally false and leaves the retryable
  branch dead. `folder_unreachable` is the sole member and maps to a retryable
  503. Two other retryable refusals do not pass through that membership test at
  all: `descendant_scan_incomplete`, raised directly by the walk, and
  `folder_changed`, raised on fingerprint mismatch, both carry `retryable=True`
  with the default 409. A transient scan failure and a transient read failure
  therefore answer with different statuses.

  The boundary sentinel is decided from a listing the walk already holds. A
  directory listing that never names `.wbuddy` cannot be a store root, so
  `_is_store_root` is spent only on the few directories whose own listing named
  it. That probe `lstat`s the component child before reading through it, so a
  redirected component is refused rather than followed and cannot expose a store
  living outside the scanned tree. Every directory the walk opens is one a
  listing admitted: an entry is queued only after `is_symlink` and the
  reparse-point attribute clear it and its device matches the root's, and
  `inspect` validates the root before the walk starts.

  The proof covers walked descendants. `_SKIP_DIRS` holds `.git`, `.wbuddy`,
  `node_modules`, `.venv`, and `vendor`, and the walk does not descend them, so
  a store beneath one of those names is not a boundary the proof reports, and
  neither is one behind a symlink, reparse point, or device boundary. `.wbuddy`
  is the one skipped name the scan still learns from: seeing it sets the flag
  that decides whether the enclosing directory is worth probing.

  Known bound: a page is terminated only between directories. Work is spent per
  directory and per entry, but the page budget is tested per directory, so one
  page spends at most the budget plus one directory's worth of work and a single
  very large listing runs to its end inside one request. The cumulative limit is
  tested after the directory charge and after every entry charge, so an enormous
  listing still stops at the threshold rather than running unbounded. Tightening
  the page boundary needs a cursor inside a directory listing, and `os.scandir`
  exposes no stable resume point, so the alternative is buffering whole listings
  to disk, which costs more than the overshoot.

  A directory whose mtime changed while it was being listed is re-queued and the
  children it yielded are dropped, so the re-read is the only thing that
  enqueues them. `_SCAN_DIRECTORY_RETRY_LIMIT` (3) caps that: a directory under
  continuous churn raises `descendant_scan_incomplete` rather than spinning. An
  entry that exists but cannot be read raises the same code, because skipping it
  could hide a store and make the proof unsound. An entry that vanished between
  the listing and the lookup is skipped, since a path that no longer exists
  cannot hold a store.

  Scan cursors live under the machine data root keyed by an opaque token, never
  beneath the selected folder. A cursor records its root and is deleted and
  refused if replayed against a different one, since a cursor holds one folder's
  pending list. Cursors are swept at the token TTL, and every sweep failure is
  swallowed: an undeleted cursor is a small leak, never a reason to refuse a
  scan. A terminal page deletes its cursor.

  Inspection stays byte-for-byte read-only on the selected folder. Store
  identity reads open SQLite with `mode=ro&immutable=1` so a browse cannot
  create or update WAL/SHM sidecars; a mutating open is deferred to the explicit
  open action.

  At the HTTP surface the fingerprint is a server precondition carried only by
  an opaque short-lived token and is stripped from the JSON response, so no host
  path or fingerprint enters a URL. A continuation token wraps the folder path
  together with the internal scan token, and an inspection token wraps the
  folder path, the classified status, and the fingerprint; `open` and
  `initialize` each reject a token whose recorded status does not authorize
  them. An expired token returns `selection_expired` as a retryable 409, which
  the surface answers by inspecting again.
---

# Co-work folder setup

Setting a folder up for Co-work writes `.wbuddy/manifest.yaml` and the canonical store at `.wbuddy/cowork/`. Before either exists, Co-work proves that the selected folder is the only Co-work folder on its own line of descent: no ancestor already holds a store, and no walked descendant already holds a `.wbuddy/cowork/store.yaml` of its own.

Overlapping stores are unresolvable rather than untidy. Two stores that both enclose a file each claim that file under a different folder-relative path, each keep their own content hash for it, and each read the other's writes as outside drift. The overlap has no correct resolution once it exists, so the boundary is established before any write rather than repaired afterwards.

## Inspection reads, setup writes

Choosing a folder inspects it. Inspection writes nothing beneath the selected folder. It reads that folder's Work Buddy data, walks its ancestors looking for an owning Co-work folder, and walks its descendants looking for a store boundary. It resolves to one of a small set of states:

- **initialized**: the folder is already a Co-work folder. The only action offered is opening it.
- **inside_existing_folder**: an ancestor is already a Co-work folder. The result names that folder and its store identity, and offers opening the owner or choosing another folder.
- **contains_nested_folder**: the descendant walk crossed at least one store boundary. The result names every boundary crossed, each with its folder name, host path, and store identity where that boundary itself reads as an initialized folder. The only action offered is choosing another folder.
- **unavailable**: the descendant walk reached its cumulative work threshold before it could answer, carrying the reason `folder_too_large_for_safe_setup`. The action offered is choosing a narrower folder inside the selected one.
- **collision**: the folder holds Work Buddy data that does not form a complete Co-work folder. A reason code separates an incomplete or redirected managed layout, store records that contradict each other, and data that could not be read at all. The actions offered are repairing that data or choosing another folder.
- **uninitialized**: an ordinary folder that can be set up.
- **inspection_pending**: the descendant walk has more tree to cover. The result carries a continuation token, a count of entries visited, and a short suggested wait. A caller asks again with that token until the walk resolves.

Setup is a separate, explicit call. It consumes the authorization a terminal inspection minted, re-walks the whole tree in one unpaged pass while holding the folder operation locks, and proceeds only if the folder still classifies as uninitialized.

Setup is all or nothing. A failure part way through removes the store directory it created, restores the exact manifest bytes it published, removes the `.wbuddy` directory when setup was what created it, and unregisters the store. A refused or failed setup leaves the folder as it was.

## Refusals name the state and the action

A caller that asks for setup on a folder that cannot take it receives a typed code, prose that names both the folder's state and the action that answers it, and an HTTP status consistent with that code:

- **`folder_already_initialized`**: the folder is already a Co-work folder, and the answer is to open it instead of setting it up again.
- **`inside_existing_folder`**: the folder sits inside a Co-work folder, and the answer is to select a folder outside that one.
- **`contains_nested_folder`**: the folder encloses a Co-work folder, and the answer is to select a folder that does not enclose it.
- **`folder_too_large_for_safe_setup`**: the folder holds too many items to check safely, and the answer is to select a narrower folder inside it.
- **`folder_layout_incomplete`**: the folder holds Work Buddy data that does not form a complete Co-work folder, and the answer is to repair that data or select a different folder.
- **`identity_conflict`**: the folder's store records were read and disagree about which store the folder holds. That is a different repair from an incomplete layout, and it is reported as its own code rather than folded into one.
- **`folder_unreachable`**: the folder's Work Buddy data could not be read.
- **`descendant_scan_incomplete`**: the descendant walk could not finish, because a descendant could not be read or kept changing while it was listed.
- **`folder_changed`**: the folder moved out from under the observation a person was shown.

The message is the whole contract for an agent caller, which reads the exception text and nothing else, so each refusal states the folder's state and the action together.

## Retryable and settled refusals

Three refusals are worth asking again unchanged. `folder_unreachable` says that reading the folder's Work Buddy data failed, so the classification settled nothing about what that data holds and the same folder can classify differently on the next attempt. `descendant_scan_incomplete` says the same thing about one descendant. `folder_changed` says the folder moved between observation and setup, and a fresh inspection can resolve it.

Every other refusal describes a fact the walk established. An already initialized folder, an enclosing or enclosed Co-work folder, a folder past the work threshold, an incomplete layout, and contradictory store records are all settled: repeating the call cannot change any of them, and only the user acting on the folder can. Those refusals carry a non-retryable status and prose that avoids transient wording, so an automatic retry layer does not queue a folder whose answer is fixed.

## An unreadable folder is unread, not broken

Failing to read the folder's Work Buddy data is a different answer from reading it and finding it damaged. `folder_unreachable` reports that Co-work cannot tell what state the folder is in and asks the caller to try again in a moment. It deliberately does not use the repair wording that a genuine collision uses, because an agent told the data is broken repairs a folder that was never shown to be broken. A healthy store held open by a backup or a scanner classifies this way, and the very next attempt is expected to succeed.
