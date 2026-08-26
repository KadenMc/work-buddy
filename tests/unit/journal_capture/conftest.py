"""Keep Journal's logical-day Settings reads inside each test's data cohort."""

from zoneinfo import ZoneInfo

import pytest

from work_buddy import config
from work_buddy.settings import store


@pytest.fixture(autouse=True)
def isolate_journal_settings(tmp_path, monkeypatch):
    store._schema_ready.clear()
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "journal-test-settings.db")
    monkeypatch.setattr(config, "_USER_TZ_CACHE", ZoneInfo("America/New_York"))
    yield
    store._schema_ready.clear()
