"""work-buddy backup infrastructure.

Three layers, each a separate module:

- ``local`` — Hot-backup each vital SQLite DB via the
  ``sqlite3.Connection.backup`` API, bundle into a tar.gz with a
  manifest, write to ``.data/backups/<isots>/``, prune old snapshots
  per a tiered retention policy. No external dependencies.

- ``remote`` (TBD) — Push the most recent local snapshot to GitHub
  Releases via the ``gh`` CLI subprocess. Mirror the local retention
  policy by deleting out-of-bucket releases.

- ``restore`` (TBD) — Pull a snapshot tarball, validate manifest +
  versions, unpack into staging, run migrations on the staging DBs,
  verify integrity + row counts, atomically swap into place.

The manifest format is in :mod:`work_buddy.backups.manifest` and
versioned independently so the schema can evolve.

See ``architecture/backups`` for the full subsystem reference.
"""
