"""work-buddy: Context bundle collector for PhD research scaffolding."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("work-buddy")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"
