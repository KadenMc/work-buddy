"""Cross-cutting storage primitives shared by every SQLite-backed module.

Currently exposes ``migrations.MigrationRunner`` — a versioned schema
migration framework with these safety properties:

- ``PRAGMA user_version`` as the authoritative current-version signal
- Atomic transaction wrapping per migration step (callable + version bump)
- ``PRAGMA foreign_keys`` discipline (set before BEGIN, not inside)
- Write-lock acquired before reading version (race-safe across processes)
- Downgrade guard (refuses to open a DB whose version exceeds known code)
- Code-hash audit (detects "someone edited a shipped migration" anti-pattern)

See ``architecture/migrations`` for the full reference.
"""
