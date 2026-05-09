"""Backend implementations for the artifact lifecycle system.

Each backend implements the ``Storage`` protocol from
:mod:`work_buddy.artifacts.protocol`. Backends are deliberately
flat — no inheritance hierarchy across backends, just a shared protocol
they each conform to. Consumers compose a backend with a lifecycle and
optional provenance to register an Artifact.
"""

from __future__ import annotations

from work_buddy.artifacts.backends.filesystem import FilesystemStorage

__all__ = ["FilesystemStorage"]
