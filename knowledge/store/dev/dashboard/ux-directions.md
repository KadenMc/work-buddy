---
name: Dashboard UX Directions
kind: directions
command: wb-dev-dashboard
description: Discover dashboard tasks independently of existing controls, walk the complete interaction, prioritize useful information, and verify outcomes with honest evidence.
parents:
- dev/dashboard
tags:
- dashboard
- ux
- development
- verification
trigger: When planning, implementing, or reviewing changes to dashboard user tasks or visible behavior, including generated surfaces.
---

Derive important scenarios independently of the current interface. Use the interface to determine which scenarios are supported, not to determine which scenarios exist. Optimize the user's ability to reach a useful outcome and understand the result.

## Establish tasks and scope

Read product intent, existing decisions, relevant feedback, requirements, and the affected App's architecture. Reuse established context. Identify who arrives, their situation, what they already know, their starting point, and their desired outcome. Do not invent personas or treat simulated reactions as user research.

Expand each primary task by considering correction, reuse, interruption and resumption, cancellation, investigation, recovery, revisiting prior work, and passing the result into another task. Inventory plausible actions on objects the user recognizes, such as a document, Folder, task, or run. Consider lifecycle state, permissions, and relationships between those objects.

These are candidate needs. Prioritize by product purpose, evidence, frequency, consequence, and the authorized scope. Surface consequential scope decisions rather than silently adding features or dropping important needs. Continue routine implementation without approval stops at ordinary milestones.

For each retained scenario record the situation, starting point, desired outcome, observable success condition, and relevant exceptional conditions. Classify it as **supported**, **missing**, **undiscoverable**, **broken**, **uncertain**, or **intentionally out of scope**. Explain the product boundary for the last classification.

## Walk the complete interaction

Trace the user goal through discoverable controls or navigation, declared interaction, host/provider/backend handling, authoritative result, and visible feedback with a next action. Respect App-owned and host-owned boundaries. Check neighboring navigation when the outcome crosses the React and Python-generated frontends.

At every step ask:

- Does the user have a reason to take this step?
- Will they notice the relevant action?
- Can they predict its effect from its label and placement?
- Can they recognize progress and tell whether it worked?

Check entry points, required information, labels, completion, next actions, recovery, and relevant empty, loading, error, partial, and success states. Prefer established interaction patterns and clear information scent. Click counts do not establish usability. Ordinary tasks must not require source knowledge, hidden routes, or undocumented gestures.

## Prioritize information by purpose

| Treatment | Purpose |
|---|---|
| Visible by default | Choose, act, understand consequential state, avoid a costly mistake, or recover. Show save state, why an action is blocked, and the next useful action. |
| Available in context | Less frequent but legitimate needs, such as history, advanced settings, or optional explanations. Use existing disclosure and Hover Help patterns. |
| Technical details or copy support details | Troubleshooting-only identifiers, build information, or stack traces. Use an explicit allowlist, success feedback, and a selectable-text fallback. Exclude secrets and document contents by default. |
| Omitted | No identified user, accountability, or support purpose. |

Metadata can be essential. Unsaved changes and failed runs belong beside the relevant action. An opaque internal identifier usually belongs in support details. Current state, validation, confirmations, required disclosures, and recovery must remain available without Hover Help. Preserve primary controls on responsive layouts.

Inspect existing notices, errors, help, and clipboard facilities before adding a shared affordance. Concrete consumers must justify it. Do not build a diagnostics platform as a prerequisite to a local improvement.

## Verify outcomes and discoverability

Follow `dev/dashboard/verification-directions`. Drive retained scenarios through the real interface in demo fixture routes or the isolated harness. Verify observable results, not merely clicks, successful requests, or rendered components. Direct endpoints can support diagnosis but do not prove a user journey works.

For a discoverability review, give a separate reviewer the goal and starting situation without the button sequence. Have them inspect the interface before reading source. Check keyboard operation and affected responsive layouts. Encode important supported outcomes in regression tests after exploration.

Label every finding and verification claim **live-verified**, **source-inferred**, **design-hypothesis**, or **untested**. Live execution proves the path worked under the tested conditions. Expert inspection can suggest learnability problems. An automated pass does not establish human learnability.

## Keep depth proportional

| Change | Review depth |
|---|---|
| Wording | Check interpretation in context with one walkthrough question. |
| Moved or restyled control | Check the scenario's entry point and discoverability. |
| New or changed mutation | Cover the full scenario, error, completion, recovery, and isolated-harness execution. |
| Shared navigation or widget infrastructure | Review affected consumers' scenarios. |
| Python registration that changes generated UI | Review the generated surface's affected scenario, such as Settings. |

No arbitrary story quota or page-long report is required. Record compact scenario coverage, prioritized blocked outcomes, repairs, information treatment decisions, and verification gaps in `dev/dashboard/ux-review`. In review-only work, report findings. When implementation is authorized, repair in-scope issues and reverify.
