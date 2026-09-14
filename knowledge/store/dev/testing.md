---
name: Testing
kind: concept
description: Testing as a development concern in work-buddy, covering how the per-surface test runners relate to each other and what a passing run does and does not prove.
summary: work-buddy is tested per surface rather than by one command. Each surface owns its runner, its isolation story, and its coverage in continuous integration, and none of them substitutes for another.
dev_notes: |-
  `tests/conftest.py` captures native roots before installing the pytest data
  override and forwards those captured roots to xdist workers. Cutover-rehearsal
  tests preserve the full native database-authority set while resolving their
  actual writes into isolated stores; this keeps hard-link and overlap guards
  meaningful without weakening the production boundary.

  The production-state canary reads SQLite in read-only, WAL-aware mode and
  compares the durable database, WAL, and rollback-journal bytes before and
  after its isolated writes. It also checks session-marker absence and hashes
  `knowledge/store.local` so user-owned local knowledge is never a test output.
tags:
- dev
- developmental
- testing
- verification
aliases:
- test suites
- running the tests
- how work-buddy is tested
parents:
- dev
---

work-buddy is tested per surface, not by one command. The Python package has a pytest suite, run with `uv run pytest` from the repo root. The React dashboard carries its own runners, its own isolation rules, and its own relationship to what continuous integration executes. Other subsystems add checks that live beside the code they cover.

Two things are worth establishing before reaching for any of them. A passing run proves only what that runner covered: the surfaces are independent, and green on one says nothing about the others. And a test that reaches a running work-buddy process reaches the user's real data unless it explicitly selected a fixture or an isolated root, so every surface has to document how it stays off production state.

The Python suite establishes that boundary in `tests/conftest.py` before test-module collection and before any `work_buddy` import. Every pytest process, including each xdist worker, receives a unique `WORK_BUDDY_DATA_DIR` below the operating system's temporary root. An explicit `WORK_BUDDY_TEST_DATA_DIR` parent is accepted only below that temporary root and only when it is disjoint from captured native state. The `real_task_store` marker bypasses the per-test task-database patch, not the process sandbox. Run `tests/unit/test_production_state_isolation.py` whenever this boundary changes; it covers native and compatibility task writes, session creation, personal knowledge, and committed WAL visibility while proving native state and `knowledge/store.local` remain unchanged.

Automated testing is distinct from live testing, which drives an in-progress change through the real running system with the user in the loop. See `dev/live-testing-directions` for that, and `dev/testing/react-dashboard` for the dashboard's five test surfaces.

For browser-driven dashboard exploration and scenario verification, load `dev/dashboard/verification-directions`. It selects an environment and the caller's browser recipe before regression tests are authored.
