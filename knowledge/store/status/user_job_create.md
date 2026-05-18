---
name: User Job Create
kind: capability
description: Author a personal scheduled cron job by writing a .md file under <data_root>/user_jobs/. Validates the cron expression and refuses to overwrite an existing job. The scheduler hot-reloads (~30s) and starts firing the job. See features/user-jobs for the schema.
capability_name: user_job_create
category: status
parameters:
  name:
    type: str
    description: Job name (becomes the filename stem); 1-64 chars, alphanumeric + - + _.
    required: true
  schedule:
    type: str
    description: 5-field cron expression (MIN HOUR DOM MON DOW).
    required: true
  job_type:
    type: str
    description: 'One of: capability, workflow, prompt.'
    required: false
  capability:
    type: str
    description: (type=capability) Registered capability name to invoke.
    required: false
  params:
    type: dict
    description: (type=capability) Parameters dict for the capability.
    required: false
  workflow:
    type: str
    description: (type=workflow) Registered workflow name to start.
    required: false
  prompt:
    type: str
    description: (type=prompt) Body text used as the agent prompt.
    required: false
  enabled:
    type: bool
    description: Whether the job is enabled at create time. Default true.
    required: false
  recurring:
    type: bool
    description: False = one-shot (schedule cleared after first fire). Default true.
    required: false
  overwrite:
    type: bool
    description: If true, replace an existing job file with the same name. Default false (refuses to overwrite). Used by the Edit-job flow in the dashboard.
    required: false
  jitter_seconds:
    type: int
    description: Optional stable jitter applied on top of cron eligibility. Non-negative integer; jobs fire at scheduled_at + a deterministic offset in [0, jitter_seconds]. Default 0 (no jitter — fire inline on cron match). Tick cadence quantizes values < ~30s away in practice; recommended floor is 60. See features/user-jobs.
    required: false
mutates_state: true
retry_policy: manual
tags:
- status
- user
- job
- create
aliases:
- create user job
- add user job
- schedule cron task
- personal job
- new scheduled job
- wb-job-new
parents:
- status
- status
---
