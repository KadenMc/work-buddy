# Frozen truth schema v1 store

This directory is the checked-in released-schema fixture required by the
`tms-glue.md` II.5 migration contract.

`store.db.sql` is a reviewable SQLite dump captured from the settled v1
migration at commit `73e2bd2a`. Tests restore it with SQLite's
`executescript`; they never call the current v1 DDL to manufacture the old
store. The dump contains one confirmed claim with fixed store, claim, gesture,
status-event, and ledger identities. `manifest.json` pins the dump digest and
those durable identities.

Do not regenerate this fixture when adding v2. Add a new frozen directory for
the newly released schema only after that schema settles.
