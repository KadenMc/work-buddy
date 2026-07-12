---
name: Cross-platform release packaging
kind: reference
description: Native Linux and macOS artifact builds plus hosted black-box installation acceptance and least-privilege release aggregation.
tags:
- dev
- packaging
- installer
- github-actions
- linux
- macos
- acceptance
aliases:
- cross-platform installer CI
- Linux installer acceptance
- macOS installer acceptance
- release packaging
parents:
- dev
entry_points:
- packaging/linux
- packaging/macos
- packaging/acceptance
- .github/workflows/cross-platform-packaging.yml
- .github/workflows/release.yml
dev_notes: |-
  Acceptance must require GITHUB_ACTIONS=true, runner.environment=github-hosted,
  non-root execution, and every mutable root beneath RUNNER_TEMP. Jobs receive
  no external credentials and default to contents: read; only the final
  draft-release job receives contents: write. Shared behavior lives in
  packaging/acceptance scripts.

  Autostart uses require mode when a hosted user service-manager session exists
  and records an explicit skip fallback otherwise. A fallback proves generated
  integration plus direct runtime behavior but must not be reported as real
  systemd/launchd lifecycle coverage.
---

work-buddy builds Linux x86-64 and Apple Silicon macOS tarballs on pinned native GitHub-hosted runners. Packaging pull requests run structural native builds; labeled, manual, and release runs can download the uploaded artifacts into separate fresh native VMs and exercise install, health, desktop launch, repair, and uninstall.

The release workflow builds the Windows setup executable and both tarballs, requires Linux/macOS acceptance for tagged releases, and attaches all accepted artifacts through one final draft-release job. The uploaded artifact, not the source checkout, is the installation subject.

Linux installs a `.desktop` entry that calls the shared self-healing launcher. macOS installs `Work Buddy.app`, whose console-less executable delegates to the same `work_buddy.desktop_launcher` module. Platform uninstall helpers delegate service/login-item/PATH teardown to `wbuddy uninstall`, remove application files, and preserve data unless remove-data is explicitly requested.
