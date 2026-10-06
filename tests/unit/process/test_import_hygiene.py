"""Importing the process package must be cheap and free of side effects.

Host entry points import it before their session identity exists, and hot
paths such as ``wbuddy hook`` import it on every call. So it may load only
the standard library, must not configure logging or create a session
directory, and must not load configuration.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_PROBE = r"""
import json, sys
before = set(sys.modules)
import work_buddy.process
loaded = sorted(set(sys.modules) - before)
print(json.dumps(loaded))
"""


def test_import_loads_only_the_standard_library_and_has_no_side_effects(tmp_path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("WORK_BUDDY_SESSION_ID", "CODEX_THREAD_ID")}
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout.strip().splitlines()[-1])
    foreign = [
        name for name in loaded
        if name.split(".")[0] not in sys.stdlib_module_names
        and name != "work_buddy"
        and not name.startswith("work_buddy.process")
    ]
    assert foreign == []
    # No logging setup, session directory, configuration or path resolution.
    for module in (
        "work_buddy.logging_config",
        "work_buddy.agent_session",
        "work_buddy.config",
        "work_buddy.paths",
    ):
        assert module not in loaded
    assert list(tmp_path.iterdir()) == []
