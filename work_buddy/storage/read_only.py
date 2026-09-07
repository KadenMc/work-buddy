"""Process-local storage posture without importing an application package."""

_enabled = False


def process_read_only() -> bool:
    return _enabled


def enable_process_read_only() -> None:
    global _enabled
    _enabled = True
