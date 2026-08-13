"""Focused contracts for Chrome mutation request payloads."""

from __future__ import annotations

from work_buddy.collectors import chrome_collector


def test_focus_or_create_normalizes_app_base_and_requests_path_preservation(
    monkeypatch,
):
    seen = {}

    def fake_request(mutation, timeout_seconds=15, **params):
        seen.update(
            mutation=mutation,
            timeout_seconds=timeout_seconds,
            params=params,
        )
        return {"status": "ok"}

    monkeypatch.setattr(chrome_collector, "_request_mutation", fake_request)

    result = chrome_collector.focus_or_create_tab(
        "http://127.0.0.1:5127/app/",
        target_hash="#wb-bootstrap=wbb_test",
        preserve_path=True,
        timeout_seconds=10,
    )

    assert result == {"status": "ok"}
    assert seen == {
        "mutation": "focus_or_create_tab",
        "timeout_seconds": 10,
        "params": {
            "url": "http://127.0.0.1:5127/app",
            "target_hash": "#wb-bootstrap=wbb_test",
            "preserve_path": True,
        },
    }
