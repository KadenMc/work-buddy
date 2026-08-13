"""Native-host request correlation for trusted dashboard tab handoffs."""

from __future__ import annotations

import json

from work_buddy.chrome_native_host import host


def test_check_passes_identity_handoff_fields_to_extension(tmp_path, monkeypatch):
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "request_id": "request-1",
                "request_action": "mutate",
                "mutation": "focus_or_create_tab",
                "url": "http://127.0.0.1:5127/app",
                "target_hash": "#wb-bootstrap=secret",
                "preserve_path": True,
            }
        ),
        encoding="utf-8",
    )
    responses = []
    monkeypatch.setattr(host, "REQUEST_FILE", request_file)
    monkeypatch.setattr(host, "write_message", responses.append)

    host.handle_check()

    assert responses == [
        {
            "status": "ok",
            "requested": True,
            "request_id": "request-1",
            "request_action": "mutate",
            "mutation": "focus_or_create_tab",
            "url": "http://127.0.0.1:5127/app",
            "target_hash": "#wb-bootstrap=secret",
            "preserve_path": True,
        }
    ]


def test_stale_export_does_not_delete_newer_request(tmp_path, monkeypatch):
    request_file = tmp_path / "request.json"
    output_file = tmp_path / "output.json"
    request_file.write_text(
        json.dumps({"request_id": "new-request"}),
        encoding="utf-8",
    )
    responses = []
    monkeypatch.setattr(host, "REQUEST_FILE", request_file)
    monkeypatch.setattr(host, "OUTPUT_FILE", output_file)
    monkeypatch.setattr(host, "write_message", responses.append)

    host.handle_export(
        {
            "action": "export",
            "request_id": "stale-request",
            "mutation_result": {"status": "ok"},
        }
    )

    assert request_file.exists()
    assert json.loads(output_file.read_text(encoding="utf-8"))["request_id"] == (
        "stale-request"
    )
    assert responses[0]["status"] == "ok"


def test_correlated_export_removes_its_request(tmp_path, monkeypatch):
    request_file = tmp_path / "request.json"
    output_file = tmp_path / "output.json"
    request_file.write_text(
        json.dumps({"request_id": "request-1"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(host, "REQUEST_FILE", request_file)
    monkeypatch.setattr(host, "OUTPUT_FILE", output_file)
    monkeypatch.setattr(host, "write_message", lambda _response: None)

    host.handle_export(
        {
            "action": "export",
            "request_id": "request-1",
            "mutation_result": {"status": "ok"},
        }
    )

    assert not request_file.exists()
