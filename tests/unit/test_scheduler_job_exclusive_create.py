from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from work_buddy.sidecar.scheduler.jobs import create_user_job_file


def test_concurrent_job_create_cannot_overwrite_same_name(tmp_path, monkeypatch):
    target = tmp_path / "same-job.md"
    barrier = Barrier(2)
    original_exists = Path.exists

    def stale_existence_check(path):
        exists = original_exists(path)
        if path == target:
            # Both requests observe a missing job before either publishes it.
            barrier.wait(timeout=10)
        return exists

    monkeypatch.setattr(Path, "exists", stale_existence_check)

    def create(prompt):
        result = create_user_job_file(
            tmp_path,
            name="same-job",
            schedule="0 9 * * 1",
            job_type="prompt",
            prompt=prompt,
            overwrite=False,
        )
        return prompt, result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["First user's job", "Second user's job"]))
    winners = [prompt for prompt, result in results if result["success"]]
    losers = [result for _, result in results if not result["success"]]
    assert len(winners) == 1
    assert len(losers) == 1
    assert "already exists" in losers[0]["error"]
    assert target.read_text(encoding="utf-8").endswith(winners[0] + "\n")


def test_explicit_legacy_job_edit_still_replaces_existing_content(tmp_path):
    body = {"name": "editable-job", "schedule": "0 9 * * 1", "job_type": "prompt"}
    assert create_user_job_file(tmp_path, **body, prompt="Original")["success"]
    assert create_user_job_file(
        tmp_path, **body, prompt="Human-authorized edit", overwrite=True
    )["success"]
    assert (
        (tmp_path / "editable-job.md")
        .read_text(encoding="utf-8")
        .endswith("Human-authorized edit\n")
    )
