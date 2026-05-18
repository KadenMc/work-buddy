---
name: Stress Test
kind: workflow
description: Subprocess isolation validation workflow (developer tool). The `compute-primes` step runs in a subprocess to exercise the gateway's subprocess execution path.
workflow_name: stress-test
execution: main
steps:
- id: compute-primes
  name: CPU stress test (primes sieve)
  step_type: code
  depends_on: []
  auto_run:
    callable: work_buddy.stress_test.compute_primes
    kwargs:
      limit: 1000000
    timeout: 60
  visibility:
    mode: full
  invokes: []
- id: verify-result
  name: Verify stress test result
  step_type: reasoning
  depends_on:
  - compute-primes
  invokes: []
tags:
- dev
- stress
- test
parents:
- dev
- dev
---
