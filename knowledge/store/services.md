---
name: Services & Infrastructure
kind: concept
description: Sidecar-managed services — dashboard, messaging, embedding, and service pointers
summary: 'work-buddy runs several sidecar-managed services: dashboard (5127), messaging (5123), embedding (5124), telegram (5125). The sidecar daemon manages lifecycle and scheduling.'
tags:
- services
- sidecar
- dashboard
- messaging
- embedding
---

Services are managed by the sidecar daemon. Each runs on a dedicated port. Use sidecar_status to check service health.
