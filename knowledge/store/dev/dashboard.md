---
name: Dashboard Development
kind: concept
description: Develop dashboard user tasks with proportional UX review, explicit verification evidence, and isolated write environments.
summary: Dashboard changes require complete user outcomes, evidence from the appropriate verification surface, and disposable data for every persisted write.
parents:
- dev
tags:
- dashboard
- development
- testing
---

Dashboard development has three obligations: understand the user task, verify its outcome, and keep persisted writes inside disposable environments.

Load `dev/dashboard/ux-directions` before deciding an interaction and `dev/dashboard/verification-directions` before opening a browser. `dev/testing/react-dashboard` describes the independent test runners. Record scoped scenario evidence through `dev/dashboard/ux-review` and reuse it in `dev-pr` when the implementation and scope still match.

Apply this guidance when a change affects user tasks or visible behavior, including generated dashboard surfaces changed by Python registrations. Unrelated development does not require a dashboard review. Depth follows the consequence of the change.
