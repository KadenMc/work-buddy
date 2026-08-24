from __future__ import annotations

from work_buddy.tasks.events import publish_pending


class FakeOutbox:
    def __init__(self) -> None:
        self.events = [
            {"event_id": "ok", "payload": {"revision": 1}},
            {"event_id": "retry", "payload": {"revision": 2}},
        ]
        self.published: list[str] = []
        self.failed: list[str] = []

    def pending_outbox(self, *, limit: int = 100):
        return self.events[:limit]

    def mark_outbox_published(self, event_id: str, *, published_at: str) -> bool:
        assert published_at
        self.published.append(event_id)
        return True

    def record_outbox_failure(self, event_id: str, *, error: str) -> bool:
        assert error
        self.failed.append(event_id)
        return True


def test_outbox_marks_only_confirmed_dashboard_delivery() -> None:
    store = FakeOutbox()
    seen: list[tuple[str, dict]] = []

    def deliver(event_type: str, payload: dict) -> bool:
        seen.append((event_type, payload))
        return payload["revision"] == 1

    result = publish_pending(store, delivery=deliver)

    assert result == {"published": 1, "failed": 1}
    assert store.published == ["ok"]
    assert store.failed == ["retry"]
    assert seen == [
        ("task.changed", {"revision": 1}),
        ("task.changed", {"revision": 2}),
    ]
