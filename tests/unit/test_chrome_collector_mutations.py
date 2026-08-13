"""Focused contracts for Chrome mutation request payloads."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

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


def test_focus_existing_uses_non_creating_mutation(monkeypatch):
    seen = {}

    def fake_request(mutation, timeout_seconds=15, **params):
        seen.update(
            mutation=mutation,
            timeout_seconds=timeout_seconds,
            params=params,
        )
        return {"status": "ok", "details": {"found": True}}

    monkeypatch.setattr(chrome_collector, "_request_mutation", fake_request)

    result = chrome_collector.focus_existing_tab(
        "http://127.0.0.1:5127/app/",
        target_hash="#wb-bootstrap=wbb_test",
        timeout_seconds=10,
    )

    assert result == {"status": "ok", "details": {"found": True}}
    assert seen == {
        "mutation": "focus_existing_tab",
        "timeout_seconds": 10,
        "params": {
            "url": "http://127.0.0.1:5127/app",
            "target_hash": "#wb-bootstrap=wbb_test",
        },
    }


def test_mutation_ignores_unrelated_shared_output_until_nonce_matches(
    tmp_path,
    monkeypatch,
):
    request_file = tmp_path / "request.json"
    output_file = tmp_path / "tabs.json"
    output_file.write_text(json.dumps({"request_id": "older"}), encoding="utf-8")
    stamp = output_file.stat().st_mtime
    monkeypatch.setattr(chrome_collector, "_REQUEST_FILE", request_file)
    monkeypatch.setattr(chrome_collector, "_TABS_FILE", output_file)
    monkeypatch.setattr(
        chrome_collector.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="request-1"),
    )
    sleeps = 0

    def advance_response(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            payload = {"request_id": "unrelated", "mutation_result": {}}
        else:
            payload = {
                "request_id": "request-1",
                "mutation_result": {
                    "status": "ok",
                    "details": {"found": True},
                },
            }
        output_file.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(output_file, (stamp + sleeps, stamp + sleeps))

    monkeypatch.setattr(chrome_collector.time, "sleep", advance_response)

    result = chrome_collector._request_mutation(
        "focus_existing_tab",
        timeout_seconds=1,
        url="http://127.0.0.1:5127/app",
    )

    assert result == {"status": "ok", "details": {"found": True}}
    request = json.loads(request_file.read_text(encoding="utf-8"))
    assert request["request_id"] == "request-1"
    assert not output_file.exists()


def test_snapshot_timeout_does_not_delete_a_newer_request(tmp_path, monkeypatch):
    request_file = tmp_path / "request.json"
    output_file = tmp_path / "tabs.json"
    monkeypatch.setattr(chrome_collector, "_REQUEST_FILE", request_file)
    monkeypatch.setattr(chrome_collector, "_TABS_FILE", output_file)
    monkeypatch.setattr(
        chrome_collector.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="snapshot-1"),
    )
    monotonic = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(
        chrome_collector.time,
        "time",
        lambda: next(monotonic, 2.0),
    )

    def replace_with_newer_request(_seconds):
        request_file.write_text(
            json.dumps({"request_id": "newer-request"}),
            encoding="utf-8",
        )

    monkeypatch.setattr(chrome_collector.time, "sleep", replace_with_newer_request)

    assert chrome_collector.request_tabs(timeout_seconds=1) is None
    assert json.loads(request_file.read_text(encoding="utf-8")) == {
        "request_id": "newer-request"
    }


def test_snapshot_accepts_an_idless_extension_response(
    tmp_path,
    monkeypatch,
):
    request_file = tmp_path / "request.json"
    output_file = tmp_path / "tabs.json"
    output_file.write_text("{}", encoding="utf-8")
    stamp = output_file.stat().st_mtime
    monkeypatch.setattr(chrome_collector, "_REQUEST_FILE", request_file)
    monkeypatch.setattr(chrome_collector, "_TABS_FILE", output_file)

    def idless_worker_response(_seconds):
        output_file.write_text(json.dumps({"tabs": [{"tabId": 1}]}), encoding="utf-8")
        os.utime(output_file, (stamp + 1, stamp + 1))

    monkeypatch.setattr(chrome_collector.time, "sleep", idless_worker_response)

    assert chrome_collector.request_tabs(timeout_seconds=1) == {
        "tabs": [{"tabId": 1}]
    }


def test_content_timeout_does_not_delete_a_newer_request(tmp_path, monkeypatch):
    request_file = tmp_path / "request.json"
    output_file = tmp_path / "tabs.json"
    monkeypatch.setattr(chrome_collector, "_REQUEST_FILE", request_file)
    monkeypatch.setattr(chrome_collector, "_TABS_FILE", output_file)
    monkeypatch.setattr(
        chrome_collector.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="content-1"),
    )
    monotonic = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(
        chrome_collector.time,
        "time",
        lambda: next(monotonic, 2.0),
    )

    def replace_with_newer_request(_seconds):
        request_file.write_text(
            json.dumps({"request_id": "newer-request"}),
            encoding="utf-8",
        )

    monkeypatch.setattr(chrome_collector.time, "sleep", replace_with_newer_request)

    assert chrome_collector.request_content([42], timeout_seconds=1) is None
    assert json.loads(request_file.read_text(encoding="utf-8")) == {
        "request_id": "newer-request"
    }
