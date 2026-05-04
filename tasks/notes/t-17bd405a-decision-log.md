---
type: decision-log
task_id: t-17bd405a
session_id: e08897a8-59c3-4cd8-8add-c13cd1043ead
started: 2026-04-25
---
# Decision log — Dashboard refresh-bug fix

User went AFK; working autonomously. Order: Decision 2 (URL hash) first, then Decision 1(b) (data-only refresh).

## Decision 2 — completed (script_main.py, script_costs.py, script_review.py)

**Files touched:**
- `work_buddy/dashboard/frontend/script_main.py` — added `_persistHash()`, `_initFromHash()`, hooked `switchTab`, `selectChat`, `selectNamespace`, `chatsJumpToHit`, `chatsJumpToCommitSearch`, restored chat from `_urlState.ci` inside `loadChats`. Replaced eager `loadOverview()` at bottom with DOMContentLoaded → `_initFromHash`.
- `work_buddy/dashboard/frontend/script_costs.py` — hooked `costsProjectChanged`, `costsRangeChanged`, `costsActivityChanged`. Added `_costsSyncActivityPills()` and `_costsSyncToolbar()` to mirror state → DOM (needed when state was set by URL restore, not user click).
- `work_buddy/dashboard/frontend/script_review.py` — added `_persistHash()` call inside `loadReview()` (which the dropdown `onchange` already invokes).

**Decisions made along the way:**

1. **`_wbHashInitInProgress` guard.** The init flow calls `switchTab(...)` which itself calls `_persistHash()`. To avoid persisting an intermediate state during restore (e.g. before chat list has loaded), wrapped the init body in a try/finally that suppresses _persistHash until init completes, then calls it once.

2. **Legacy `#view/<id>` is left untouched.** `script_workflows.handleHashRoute()` already routes that format. `_initFromHash()` bails out if `hash.match(/^#view\//)`, so legacy deep-links keep working until the user interacts (at which point _persistHash will rewrite to the new `#tab=ntf&ntf=<id>` format).

3. **Chat selection restore is one-shot via `window._urlState.ci`.** Set in `_initFromHash`, consumed and `delete`'d inside `loadChats` after the chat list is fetched (need it before short_id → full session_id resolution). Other keys (`cp`, `cr`, `ca`, `tn`, `rs`) are applied directly to module state / DOM in `_initFromHash` synchronously.

4. **Cost toolbar widget sync.** Added `_costsSyncToolbar()` (called from `costsLoadProjects`) and `_costsSyncActivityPills()`. Without these, hash-restored state would leave the range select and pill `.active` class out of sync with `costsState`.

5. **Q3 implemented:** No-hash startup calls `switchTab('overview')` which triggers `_persistHash` → writes `#tab=overview` eagerly.

## Decision 1(b) — completed (script_main.py, script_costs.py)

**Files touched:**
- `script_main.py` — added `dataRefreshers` table + refactored `startAutoRefresh` to call `dataRefreshers[tab]()` instead of `switchTab(tab)`. Added `_selectedProjectSlug` + selection-preserve in `renderProjectList` so project highlight survives auto-refresh.
- `script_costs.py` — added `refreshCostsData()` (re-fetches /api/costs and renders data-only — preserves `selectedModels`, skips chip rebuild). Added `opts.skipModelsFilter` to `costsRenderAll`.

**Per-tab decisions:**
- `overview`, `status`, `contracts`, `review` — loaders are already pure data renders (no toolbar rebuild); aliased directly.
- `tasks` — alias to `_refreshTaskView` (skips `_renderTaskStateChips()` so chips/search input keep state).
- `chats` — alias to `loadChats` (re-renders left list; right viewer is separate DOM and survives).
- `projects` — alias to `loadProjects` (only writes to left list; right detail pane untouched). Added `_selectedProjectSlug` to preserve highlight.
- `costs` — dedicated `refreshCostsData()` per spec.
- `settings` — alias to `loadSettings`. The handoff said the existing snapshot/restore code in script_settings.py covers the destructive parts; cleanup is a separate task.

**Note added during work:** workflow tabs (`wv-*`) skip the auto-refresh entirely (was already true).

## Final state — committed

Single commit (per handoff's "your call"): `cd73918 fix(dashboard): kill the chronic refresh-bug at the source`. 3 files changed, 246 insertions(+), 6 deletions(-).

**Verification done:**
- `render_page()` succeeds (HTTP 200 from a temporary alt-port instance).
- `node --check` passes on the rendered JS.
- All probed API endpoints (/api/state, /api/tasks, /api/chats, /api/review, /api/costs/projects) return 200.
- Static reading of every changed function for control-flow correctness.

**Live in-browser verification (post-sidecar-restart, via Claude_Preview eval):**

- Initial load: page renders, `#tab=overview` is written eagerly (Q3 ✓), all new symbols present at global scope (`_persistHash`, `_initFromHash`, `dataRefreshers` with all 9 keys, `refreshCostsData`).
- Hash writes: `switchTab('costs')` → `#tab=costs&cr=30`. Setting `costsState.project='work-buddy'`, `range='7'`, `activity='programmatic'` then `_persistHash()` → `#tab=costs&cp=work-buddy&cr=7&ca=programmatic`.
- **Full-state restore on real reload**: with hash `#tab=costs&cp=work-buddy&cr=7&ca=programmatic`, `window.location.reload()` → after reload: active tab=costs, project select="work-buddy", range select="7", activity pill "programmatic" visible+active, `costsState` matches the URL.
- **Data-only refresh proven non-destructive**: stamped first child of `#costs-models-filter`. After `refreshCostsData()`: same node, stamp survived. After `loadCosts(true)`: node was REPLACED, stamp gone. The skip flag works exactly as spec'd.
- **Legacy `#view/<id>` bailout**: with `#view/non-existent-view-id`, `_initFromHash` does NOT change the active tab and does NOT rewrite the hash — leaves the legacy handler in charge.
- **Chats restore (`ci` short_id → full UUID)**: hash `#tab=chats&ci=e08897a8`, reload → `chatsState.selectedId` resolved to full UUID, `.chat-card.active` highlighted on that card, viewer mounted.
- **Tasks restore (`tn` URL-encoded)**: hash `#tab=tasks&tn=projects/work-buddy/surfaces/dashboard`, reload → `_selectedNamespace` set, breadcrumb rendered, tree node selected.
- **`startAutoRefresh` wiring**: source contains `dataRefreshers`, does NOT contain `switchTab`. The destructive call site is gone.
- Console clean throughout — no warnings or errors emitted during any of the above.

**Verification NOT done (requires interactive browser):**
- Real user flow: pick costs project → save a .py file (Werkzeug restart) → confirm filter survives.
- Cmd-R reload on Costs tab → confirm same filter restored from URL hash.
- Open a chat → reload → confirm same chat opens.
- 30s tick on Costs tab → confirm models-chip hover state isn't dropped mid-hover.
- Legacy `#view/<id>` deep-link still routes correctly (added bail-out in `_initFromHash` but didn't exercise live).

The user said earlier they keep --dev on permanently and prefer to verify hot-reload is invisible after this. That's the smoke they should run when they return.

## Below: prior plan (kept for posterity)

Plan:
- Add `dataRefreshers` dict alongside `staticLoaders` in script_main.py.
- For each static tab, add a `refreshXData()` sibling to its `loadX()` that re-fetches and renders ONLY the data sections (no toolbar/filter rebuild).
- Refactor `startAutoRefresh` to call `dataRefreshers[tab]()` instead of `switchTab(tab)`.
- Don't touch workflow views (`wv-*`) — they already skip auto-refresh.

Per-tab data-only sketch:
- **overview**: `loadOverview` already just fetches /api/state and rewrites cards. The toolbar is the tab bar (not panel-internal), so loadOverview can BE its own refresher, OR a thin wrapper. Just alias.
- **costs**: `refreshCostsData = () => loadCosts(true)` minus the projects-dropdown reload — actually `loadCosts` re-runs `costsLoadProjects` (idempotent due to `costsState.projectsLoaded` guard) then re-fetches and re-renders. Most renderers update specific DOM regions. The only thing that's "toolbar-ish" inside costsRenderAll is `costsRenderModelsFilter` — wraps chips. Per spec: data-only = costsRenderAll minus costsRenderModelsFilter.
- **tasks**: `refreshTasksData = _refreshTaskView` (already data-only — re-renders task list + counts + tree, doesn't touch the search input or chip toolbar). Need to verify `_renderTaskStateChips` isn't called inside it (which would rebuild chips).
- **chats**: `refreshChatsData` = re-fetch chat list and re-render the list pane only; do NOT re-render the message viewer (preserves selectedId, scroll, in-progress role filters).
- **status**: loadStatus already builds health card sections. The toolbar at the top is just the section header — no inputs. Probably loadStatus is fine as-is.
- **review**: re-fetches via loadReview; potentially destructive on the renderer's internal state. Leave as full reload OR confirm renderTriageReview re-render is non-destructive (TBD).
- **contracts/projects/settings**: TBD — likely tolerable.

Open question: do I need to walk every tab carefully, or use a "safest possible default" approach for the less-touched tabs? Given the user is AFK, my plan: alias `loadX` for tabs whose loaders are already data-only (no destructive toolbar rebuild), and write proper `refreshXData` for the few that aren't (chats, costs).

## Action items

- [x] Read script_main.py
- [x] Read script_costs.py + script_workflows.py + script_review.py
- [x] Implement Decision 2
- [x] Smoke-test Decision 2 (server boots, page renders 200, JS syntax clean)
- [ ] Implement Decision 1(b): dataRefreshers + refreshXData() siblings
- [ ] Smoke-test Decision 1(b)
- [ ] Commit

## Open questions / things user may want to review when back

- Did I get the right set of "destructive vs preservable" call sites for chats? I added `_persistHash()` to `selectChat`, `chatsJumpToCommitSearch`, `chatsJumpToHit`. There may be other UX state (search query, filter) that the user wants persisted but I left out — spec says no (`tq` was Q2 = NO).
- Note: I extended `_costsSyncActivityVisibility` to also sync the pill `.active` class. This is a small behavior change beyond the spec — the pill row's pill class was previously only updated on click. New behavior: synced on every render. Should be a no-op when state matches DOM (the common case), but worth flagging.
- Hash format change: old `#view/<id>` deep links still work but on first interaction they get rewritten to `#tab=ntf&ntf=<id>`. Per spec this is fine.
