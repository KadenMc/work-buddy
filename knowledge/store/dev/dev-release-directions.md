---
name: Dev Release Directions
kind: directions
description: 'How to run /wb-dev-release: preflight gates (releasable main, version/tag/CHANGELOG agreement, Obsidian-plugin pin check), consent-gated tag push, release-CI watch, draft verification, human-only publish.'
summary: /wb-dev-release cuts a tagged release through a DAG of gates. The version bump and CHANGELOG land through /wb-dev-pr BEFORE running it; the tag push needs explicit user consent; the draft release is always published by the user, never the agent.
trigger: When the user asks to cut, tag, or publish a work-buddy release, or invokes /wb-dev-release
command: wb-dev-release
workflow: dev/dev-release
tags:
- dev
- release
- git
- packaging
- directions
aliases:
- release directions
- how to release work-buddy
- cut a release directions
parents:
- dev
---

Run `/wb-dev-release` to cut a tagged work-buddy release. The workflow enforces assess (releasable main) → version gate → cross-repo pin check → consent-gated tag push → release-CI watch → draft verification → human publish → post-verify.

Release CI builds the Windows setup executable, Linux x86-64 tarball, and Apple Silicon macOS tarball. Linux and macOS acceptance jobs download those uploaded artifacts into separate fresh native GitHub-hosted runners and exercise installation, runtime health, desktop launch, repair, and uninstall before one final job can attach the artifacts to the draft release. Draft verification requires all three platform artifacts.

## Before you run it

The workflow verifies; it does not edit. Two things must already be true, both landed through `/wb-dev-pr` like any other change:

- `pyproject.toml` carries the version you intend to tag (the release CI aborts on any mismatch; `packaging/version.py` is the single source of truth).
- `CHANGELOG.md` has an entry for that version (user-visible changes, not commit archaeology).

## The two human boundaries

1. **The tag push** starts the public release machinery, so the workflow asks for explicit consent naming the exact version before `git push origin <tag>`.
2. **Publishing the draft is always the user's click.** The workflow builds and verifies a DRAFT release; `gh release edit --draft=false` must never run from an agent. This mirrors the merge rule: agents prepare and verify, the user performs outward-facing acts.

## Cross-repo pins are a release-day concern

work-buddy and the Obsidian plugin cap each other's versions (`PLUGIN_VERSION_MIN`/`MAX` in `work_buddy/obsidian/bridge.py`, enforced with a raise; `WB_VERSION_MAX` in the plugin, advisory only). The `cross_repo` step checks the released plugin sits inside the bridge's accepted range (blocking) and whether this release trips the plugin's advisory cap (cosmetic, note-and-follow-up). The two MAX constants cap each other's projects, so either side crossing its counterpart's cap requires moving that constant in the same change.

## When a release build fails

Never leave a tag pointing at a commit whose release build failed: delete the remote tag, fix through `/wb-dev-pr`, re-run the workflow from the top. The tag is cheap; a broken tagged release is not.
