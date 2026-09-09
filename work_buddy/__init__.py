"""work-buddy: Context bundle collector for PhD research scaffolding.

The package bootstrap also establishes conservative defaults for native
numerical runtimes.  This file is imported before any ``work_buddy.*`` module,
including every supported ``python -m work_buddy.<service>`` entry point, so it
is the one reliable place to configure OpenBLAS before NumPy initializes it.
"""

import os as _os

# Work Buddy's vector operations are request-sized and run in several separate
# long-lived Python processes.  Letting each NumPy import create the machine-wide
# OpenBLAS worker pool commits hundreds of MiB per process without a material
# scoring benefit.  ``setdefault`` is intentional: an operator benchmarking a
# different value remains authoritative.
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("work-buddy")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"
