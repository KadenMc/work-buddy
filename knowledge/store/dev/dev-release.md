---
name: Dev Release
kind: workflow
description: Cut a tagged three-platform work-buddy release with preflight gates, native artifact acceptance, one draft-release aggregation job, and a human-only publish step.
workflow_name: dev-release
execution: main
allow_override: false
steps:
- id: assess
  name: Verify the repo is releasable (main, clean, synced, CI green)
  step_type: reasoning
  depends_on: []
  result_schema:
    required_keys:
    - on_main
    - clean
    - synced
    - ready
    key_types:
      on_main: bool
      clean: bool
      synced: bool
      ready: bool
  invokes: []
- id: version_gate
  name: Version, tag, and CHANGELOG agree
  step_type: reasoning
  depends_on:
  - assess
  result_schema:
    required_keys:
    - version
    - ready
    key_types:
      version: str
      ready: bool
  invokes: []
- id: cross_repo
  name: Check companion-repo version pins (Obsidian plugin)
  step_type: reasoning
  depends_on:
  - version_gate
  result_schema:
    required_keys:
    - plugin_in_range
    - blocking
    key_types:
      plugin_in_range: bool
      blocking: bool
  invokes: []
- id: tag_push
  name: Tag and push (requires explicit user consent)
  step_type: reasoning
  depends_on:
  - cross_repo
  result_schema:
    required_keys:
    - tagged
    - tag
    key_types:
      tagged: bool
      tag: str
  invokes: []
- id: watch_ci
  name: Watch the release workflow to completion
  step_type: reasoning
  depends_on:
  - tag_push
  result_schema:
    required_keys:
    - ci_green
    key_types:
      ci_green: bool
  invokes: []
- id: verify_draft
  name: Verify the draft release and its installer assets
  step_type: reasoning
  depends_on:
  - watch_ci
  result_schema:
    required_keys:
    - draft_ok
    key_types:
      draft_ok: bool
  invokes: []
- id: publish_handoff
  name: Hand the draft to the user to publish (never publish yourself)
  step_type: reasoning
  depends_on:
  - verify_draft
  result_schema:
    required_keys:
    - published
    key_types:
      published: bool
  invokes: []
- id: post_verify
  name: Verify the published release resolves publicly
  step_type: reasoning
  depends_on:
  - publish_handoff
  invokes: []
command: wb-dev-release
tags:
- dev
- release
- git
- packaging
- installer
- workflow
aliases:
- cut a release
- tag a release
- release work-buddy
- dev-release workflow
parents:
- dev
dev_notes: |-
  All steps are reasoning steps: the checks are a handful of git/gh one-liners
  whose value is the enforced sequence and auditable skips, not offloaded
  compute. If the workflow proves high-frequency, the deterministic gates
  (assess, version_gate, draft-asset check) are natural auto_run candidates in
  a `work_buddy.dev.release` module with unit tests.

  The publish boundary is deliberate and mirrors the merge rule: agents
  prepare and verify, the user performs the outward-facing act. `gh release
  edit --draft=false` must never appear in an agent's hands.

  The failure path for a red release CI is to delete the remote tag
  (`git push origin :refs/tags/<v>`), fix on a branch through /wb-dev-pr, and
  re-run this workflow from the top, so a tag never points at a commit whose
  release build failed.
---

Cut a tagged work-buddy release: preflight gates (releasable main, version/tag/CHANGELOG agreement, companion-repo pin check), a consent-gated tag push, a watch of the release CI, verification of the draft release's installer assets, and an explicit human publish step.

## Philosophy

Releases are rarer than commits, which makes a prose checklist worse, not better: nobody reliably remembers a multi-step ritual performed every few months. The same reasoning that produced the dev-pr workflow applies with more force here, plus one release-specific rule: the workflow produces and verifies a DRAFT, and publishing it is always the user's click, because publishing is an outward-facing act on the project's public face.

## What this workflow is NOT

- Not a version-bumping tool. Deciding the version (and landing the bump through /wb-dev-pr) happens before this workflow runs; the gates here verify agreement, they do not edit files.
- Not a package publisher. It watches the repo's own release CI; it does not upload artifacts anywhere itself.
- Not a substitute for the release CI's own guards (the tag-matches-pyproject assertion stays in the workflow file).

## assess

Reasoning step. Verify the repo is releasable:

```bash
git checkout main && git pull origin main
git status --short          # must be empty
git log origin/main..main   # must be empty (nothing unpushed)
gh run list --branch main --limit 3   # latest test run on main must be green
```

Advance with `{"on_main": true, "clean": true, "synced": true, "ready": true}` only when all hold. Any false value: fix first (commit or stash strays, push or pull, chase the red CI) and re-check. Set `ready: false` only if the user aborts the release.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`): FastMCP silently drops unknown kwargs.

## version_gate

Reasoning step. The intended tag, `pyproject.toml`, and the CHANGELOG must agree before anything is tagged:

```bash
uv run python packaging/version.py     # the version the release will carry
git tag -l "<version>"                 # must be EMPTY (tag not already used)
git ls-remote --tags origin "<version>"  # must be EMPTY too
grep -n "<version>" CHANGELOG.md       # an entry for this version must exist
```

The release CI hard-aborts when the tag differs from the pyproject version, so a mismatch here means the version bump has not landed yet: stop, land it through /wb-dev-pr, and restart this workflow. If the CHANGELOG lacks an entry, write one now (user-visible changes, not commit archaeology), land it the same way, and restart.

Advance with `{"version": "<x.y.z>", "ready": true}`.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`): FastMCP silently drops unknown kwargs.

## cross_repo

Reasoning step. work-buddy and its Obsidian plugin declare a two-way compatibility range; verify the release does not step outside it.

- **The enforced direction (blocking):** `work_buddy/obsidian/bridge.py` defines `PLUGIN_VERSION_MIN` (inclusive) and `PLUGIN_VERSION_MAX` (exclusive), and `require_available()` raises outside that range. Fetch the plugin's latest released version (`gh release view --repo KadenMc/obsidian-work-buddy --json tagName`) and assert `MIN <= released < MAX`. Out of range means shipped work-buddy cannot talk to the shipped plugin: **blocking**, resolve before tagging.
- **The courtesy direction (advisory):** the plugin's `WB_VERSION_MAX` (in its `src/handlers.ts`) is the first work-buddy version it has not been tested against. If the version being released is `>= WB_VERSION_MAX`, the plugin will log a cosmetic "outdated" warning against this release; work-buddy never reads that field, so this does not block. Note it and recommend a plugin-side bump plus plugin release as a follow-up.
- **The paired-constants rule:** `PLUGIN_VERSION_MAX` (bridge.py) and `WB_VERSION_MAX` (plugin) each cap the OTHER project's version, so a major-line bump on either side requires moving the counterpart's constant in the same change. In particular, releasing a plugin version at or above `PLUGIN_VERSION_MAX` gets hard-rejected by the bridge until bridge.py moves.
- The Thunderbird extension carries no version coupling to work-buddy; nothing to check.

Advance with `{"plugin_in_range": true, "blocking": false, "notes": "<advisory findings, if any>"}`.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`): FastMCP silently drops unknown kwargs.

## tag_push

Reasoning step. Pushing a tag starts the release machinery: **ask the user for explicit consent first**, naming the exact version. On yes:

```bash
git tag <version>
git push origin <version>
```

Advance with `{"tagged": true, "tag": "<version>"}`. If the user declines, advance with `{"tagged": false, "tag": ""}` and let the workflow end there.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`): FastMCP silently drops unknown kwargs.

## watch_ci

Reasoning step. The tag triggers the release workflow: Windows installer compilation; native Linux x86-64 and macOS arm64 tarball builds; downloaded-artifact install, launch, repair, and uninstall acceptance on fresh hosted runners; then one draft-release aggregation job. Watch it:

```bash
gh run list --workflow release.yml --limit 1
gh run watch <run-id> --exit-status   # or poll gh run view <run-id>
```

On success advance with `{"ci_green": true}`.

On failure: diagnose from `gh run view <run-id> --log-failed`. Do not leave a tag pointing at a commit whose release build failed: delete the remote tag (`git push origin :refs/tags/<version>` and `git tag -d <version>`), land the fix through /wb-dev-pr, and re-run this workflow from the top. Advance with `{"ci_green": false, "notes": "<what failed and where the fix went>"}`.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`): FastMCP silently drops unknown kwargs.

## verify_draft

Reasoning step. The CI creates a DRAFT release. Verify it before handing it to the user:

```bash
gh release view <version> --json isDraft,name,assets
```

Check: it is a draft; the name matches the version; and all accepted artifacts are present with plausible sizes: `work-buddy-<version>-setup.exe`, `work-buddy-<version>-linux-x86_64.tar.gz`, and `work-buddy-<version>-macos-arm64.tar.gz`. Write or polish the release notes body now (`gh release edit <version> --notes-file ...`): user-visible changes, installation instructions, supported architectures, and the unsigned macOS Gatekeeper expectation.

Advance with `{"draft_ok": true, "assets": ["..."]}`.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`): FastMCP silently drops unknown kwargs.

## publish_handoff

Reasoning step. **Publishing is the user's act, always.** Present the draft URL, the verified assets, and the release notes, then ask the user to review and publish it in the GitHub UI (or explicitly decline/postpone). Never run `gh release edit --draft=false` yourself, under any phrasing of user intent short of them publishing it with their own click.

Advance with `{"published": true}` once the user confirms it is live, or `{"published": false, "notes": "postponed"}` if they defer (the workflow then records the deferral and ends at post_verify with a no-op).

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`): FastMCP silently drops unknown kwargs.

## post_verify

Reasoning step. Only meaningful after a publish:

```bash
gh release view --repo <owner>/work-buddy latest --json tagName   # resolves to this version
curl -sI https://github.com/<owner>/work-buddy/releases/latest | head -3
```

Confirm `releases/latest` resolves to the new tag (this is what the README's install link points at), and report the final summary: version, release URL, assets, and any advisory follow-ups from cross_repo (e.g. a recommended plugin-side bump). If publishing was deferred, state what remains and where the draft lives.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`): FastMCP silently drops unknown kwargs.
