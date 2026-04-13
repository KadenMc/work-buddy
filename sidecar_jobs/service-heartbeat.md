---
schedule: "*/3 * * * *"  # every 3 minutes
recurring: true
type: capability
capability: sidecar_status
---
Periodic service health check. Calls sidecar_status to verify messaging
and embedding are healthy. Currently just logs the result — future: send
a notification to the user when a service is down or has crashed.
