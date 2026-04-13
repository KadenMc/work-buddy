---
schedule: "0 3,15 * * *"  # 3 AM and 3 PM
recurring: true
type: capability
capability: artifact_cleanup
params:
  dry_run: false
---
Daily artifact lifecycle sweep. Deletes all artifacts past their TTL-based
expiry time. Each artifact type has a different default TTL:
- context: 7 days
- export: 90 days
- report: 30 days
- snapshot: 14 days
- scratch: 3 days
