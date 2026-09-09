"""Process-boundary regressions for Work Buddy's native-memory defaults."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _fresh_python(code: str, *, openblas_threads: str | None) -> dict:
    env = os.environ.copy()
    if openblas_threads is None:
        env.pop("OPENBLAS_NUM_THREADS", None)
    else:
        env["OPENBLAS_NUM_THREADS"] = openblas_threads
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=_REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_package_bootstrap_limits_real_openblas_pool_and_scores_quickly():
    """The default must reach the native runtime, not just set an env string."""
    result = _fresh_python(
        """
        import json
        import os
        import time
        import work_buddy
        import numpy as np
        from threadpoolctl import threadpool_info
        from work_buddy.index.encode import score_dense

        candidates = np.ones((20_000, 768), dtype=np.float32)
        query = np.ones(768, dtype=np.float32)
        doc_ids = [f"doc-{i}" for i in range(len(candidates))]
        started = time.perf_counter()
        scores = score_dense(query, candidates, doc_ids)
        elapsed = time.perf_counter() - started
        openblas = [
            item for item in threadpool_info()
            if item.get("internal_api") == "openblas"
        ]
        print(json.dumps({
            "env": os.environ.get("OPENBLAS_NUM_THREADS"),
            "threads": [item["num_threads"] for item in openblas],
            "elapsed": elapsed,
            "score_count": len(scores),
            "last": scores[doc_ids[-1]],
        }))
        """,
        openblas_threads=None,
    )
    assert result["env"] == "1"
    assert result["threads"], "test environment must expose the OpenBLAS runtime"
    assert result["threads"] == [1] * len(result["threads"])
    assert result["score_count"] == 20_000
    assert result["last"] == 1.0
    # Wide enough for heavily shared CI, but catches accidental scalar/Python scoring.
    assert result["elapsed"] < 5.0


def test_package_bootstrap_preserves_operator_native_thread_override():
    result = _fresh_python(
        """
        import json
        import os
        import work_buddy
        import numpy as np
        from threadpoolctl import threadpool_info

        np.ones((8, 8), dtype=np.float32) @ np.ones(8, dtype=np.float32)
        openblas = [
            item for item in threadpool_info()
            if item.get("internal_api") == "openblas"
        ]
        print(json.dumps({
            "env": os.environ.get("OPENBLAS_NUM_THREADS"),
            "threads": [item["num_threads"] for item in openblas],
        }))
        """,
        openblas_threads="3",
    )
    assert result["env"] == "3"
    assert result["threads"] == [3] * len(result["threads"])


def test_lmstudio_liveness_probe_does_not_import_numpy():
    """Component discovery must not initialize BLAS merely to check an HTTP URL."""
    result = _fresh_python(
        """
        import http.client
        import json
        import sys
        import socket

        class FakeSocket:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False

        class FakeResponse:
            status = 200
            def read(self):
                return b'{}'

        class FakeConnection:
            def __init__(self, *_args, **_kwargs):
                pass
            def request(self, *_args, **_kwargs):
                pass
            def getresponse(self):
                return FakeResponse()
            def close(self):
                pass

        socket.create_connection = lambda *_args, **_kwargs: FakeSocket()
        http.client.HTTPConnection = FakeConnection

        from work_buddy.tools import _probe_lmstudio
        from work_buddy.health.checks import check_lmstudio
        ok = _probe_lmstudio()
        health = check_lmstudio()
        print(json.dumps({
            "ok": ok,
            "health_ok": health.get("ok"),
            "model_ids": health.get("model_ids"),
            "numpy_imported": "numpy" in sys.modules,
        }))
        """,
        openblas_threads=None,
    )
    assert result == {
        "ok": True,
        "health_ok": True,
        "model_ids": [],
        "numpy_imported": False,
    }
