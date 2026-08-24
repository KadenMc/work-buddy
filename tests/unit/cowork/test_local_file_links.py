"""Security and HTTP contract for metadata-only Co-work local-file links."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pytest
from flask import Flask

from work_buddy.cowork.folder_api import PICKER_INTENT_HEADER
from work_buddy.cowork.local_files import (
    DefaultLocalFileOsActions,
    LOCAL_FILE_OPEN_INTENT,
    LOCAL_FILE_REVEAL_INTENT,
    LocalFileLinkError,
    LocalFileLinkRegistry,
    create_local_file_blueprint,
    normalize_local_relative_path,
    parse_local_file_href,
)
from work_buddy.tasks.store import TaskStore


STORE_ID = "a" * 32
DOCUMENT_ID = "b" * 32
OTHER_DOCUMENT_ID = "c" * 32
ROOT_ID = "root_" + "d" * 27
PDF_LINK_ID = "pdf_" + "e" * 28
PPK_LINK_ID = "ppk_" + "f" * 28


@dataclass
class RecordingOsActions:
    opened: list[Path] = field(default_factory=list)
    revealed: list[Path] = field(default_factory=list)

    def open_pdf(self, path: Path) -> None:
        self.opened.append(path)

    def reveal(self, path: Path) -> None:
        self.revealed.append(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry(tmp_path: Path) -> tuple[LocalFileLinkRegistry, Path, Path]:
    root = tmp_path / "frozen" / "tasks"
    root.mkdir(parents=True)
    pdf = root / "references" / "throwaway.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-throwaway-local-link\n")
    ppk = root / "credentials" / "throwaway.ppk"
    ppk.parent.mkdir()
    ppk.write_bytes(b"PuTTY-User-Key-File-3: throwaway\n")
    catalog = tmp_path / "catalog.db"
    TaskStore(catalog).initialize()
    registry = LocalFileLinkRegistry(
        catalog,
        tmp_path / "runtime" / "roots.db",
    )
    registry.register_root(
        root_id=ROOT_ID,
        root=root,
        label="Frozen task files",
        manifest_sha256="1" * 64,
    )
    registry.register_link(
        link_id=PDF_LINK_ID,
        task_id=None,
        store_id=STORE_ID,
        document_id=DOCUMENT_ID,
        root_id=ROOT_ID,
        relative_path="references/throwaway.pdf",
        display_name="Reference PDF",
        suffix=".pdf",
        media_type="application/pdf",
        byte_length=pdf.stat().st_size,
        sha256=_sha256(pdf),
        sensitivity="ordinary",
        allowed_action="open",
        source_receipt_id="receipt-pdf",
    )
    registry.register_link(
        link_id=PPK_LINK_ID,
        task_id=None,
        store_id=STORE_ID,
        document_id=DOCUMENT_ID,
        root_id=ROOT_ID,
        relative_path="credentials/throwaway.ppk",
        display_name="Credential key",
        suffix=".ppk",
        media_type="application/x-putty-private-key",
        byte_length=ppk.stat().st_size,
        sha256=_sha256(ppk),
        sensitivity="credential",
        allowed_action="reveal",
        source_receipt_id="receipt-ppk",
    )
    return registry, pdf, ppk


def _client(
    registry: LocalFileLinkRegistry,
    *,
    membership=lambda store_id, document_id: (
        store_id == STORE_ID and document_id in {DOCUMENT_ID, OTHER_DOCUMENT_ID}
    ),
):
    actions = RecordingOsActions()
    authority_calls: list[tuple[str, str, str, Mapping[str, Any]]] = []

    def authority(
        operation: str,
        store_id: str,
        document_id: str,
        body: Mapping[str, Any],
    ) -> None:
        authority_calls.append((operation, store_id, document_id, dict(body)))

    app = Flask(__name__)
    app.register_blueprint(
        create_local_file_blueprint(
            registry_factory=lambda: registry,
            document_membership=membership,
            human_authority=authority,
            os_actions=actions,
        )
    )
    return app.test_client(), actions, authority_calls


def _url(link_id: str, *, document_id: str = DOCUMENT_ID) -> str:
    return (
        f"/api/truth/doc/{document_id}/local-files/{link_id}/activate"
        f"?store_id={STORE_ID}"
    )


def _headers(intent: str) -> dict[str, str]:
    return {
        PICKER_INTENT_HEADER: intent,
        "Origin": "http://localhost",
        "Sec-Fetch-Site": "same-origin",
    }


def test_exact_uri_parser_rejects_paths_queries_and_short_ids() -> None:
    assert parse_local_file_href(f"wb-local-file:{PDF_LINK_ID}") == PDF_LINK_ID
    assert parse_local_file_href("file:///tmp/secret.ppk") is None
    assert parse_local_file_href("wb-local-file:short") is None
    assert parse_local_file_href(f"wb-local-file:{PDF_LINK_ID}?download=1") is None
    assert parse_local_file_href(f"wb-local-file://{PDF_LINK_ID}") is None


@pytest.mark.parametrize(
    "value",
    [
        "../escape.pdf",
        "nested/../../escape.pdf",
        "/absolute/file.pdf",
        r"C:\absolute\file.pdf",
        r"\\server\share\file.pdf",
        r"\\?\C:\device\file.pdf",
        "folder/file.pdf:stream",
        "folder/%2e%2e/escape.pdf",
        "folder/%252e%252e/escape.pdf",
        "folder/.wbuddy/file.pdf",
        "folder\x00/file.pdf",
    ],
)
def test_path_normalization_rejects_traversal_devices_ads_and_encoding(
    value: str,
) -> None:
    with pytest.raises(LocalFileLinkError) as raised:
        normalize_local_relative_path(value)
    assert raised.value.code == "invalid_local_file_path"


def test_registration_rejects_symlink_or_reparse_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "outside.pdf"
    target.write_bytes(b"outside")
    link = root / "alias.pdf"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlink creation is unavailable: {exc}")
    catalog = tmp_path / "catalog.db"
    TaskStore(catalog).initialize()
    registry = LocalFileLinkRegistry(catalog, tmp_path / "roots.db")
    registry.register_root(
        root_id=ROOT_ID,
        root=root,
        label="Frozen task files",
        manifest_sha256="1" * 64,
    )
    with pytest.raises(LocalFileLinkError) as raised:
        registry.register_link(
            link_id=PDF_LINK_ID,
            task_id=None,
            store_id=STORE_ID,
            document_id=DOCUMENT_ID,
            root_id=ROOT_ID,
            relative_path="alias.pdf",
            display_name="Alias",
            suffix=".pdf",
            media_type="application/pdf",
            byte_length=target.stat().st_size,
            sha256=_sha256(target),
            sensitivity="ordinary",
            allowed_action="open",
            source_receipt_id="receipt-alias",
        )
    assert raised.value.code in {"invalid_local_file_path", "local_file_unavailable"}


def test_resolver_does_not_invent_a_parallel_task_catalog(tmp_path: Path) -> None:
    catalog = tmp_path / "uninitialized-task.db"
    registry = LocalFileLinkRegistry(catalog, tmp_path / "roots.db")
    with pytest.raises(LocalFileLinkError) as raised:
        registry.list_document_links(store_id=STORE_ID, document_id=DOCUMENT_ID)
    assert raised.value.code == "local_file_catalog_unavailable"
    with sqlite3.connect(catalog) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "task_local_file_links" not in names


def test_metadata_read_returns_no_paths_hashes_or_bytes(tmp_path: Path) -> None:
    registry, pdf, ppk = _registry(tmp_path)
    client, _, _ = _client(registry)
    response = client.get(
        f"/api/truth/doc/{DOCUMENT_ID}/local-files?store_id={STORE_ID}",
        headers={"Host": "localhost"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert len(payload["links"]) == 2
    serialized = response.get_data(as_text=True)
    assert str(pdf) not in serialized
    assert str(ppk) not in serialized
    assert _sha256(pdf) not in serialized
    assert pdf.read_text() not in serialized
    assert ppk.read_text() not in serialized
    assert {item["availability"] for item in payload["links"]} == {"verified"}
    assert all(item["local_action_available"] for item in payload["links"])


def test_absolute_root_is_machine_local_not_in_canonical_task_catalog(
    tmp_path: Path,
) -> None:
    registry, pdf, _ = _registry(tmp_path)
    with sqlite3.connect(registry.catalog_path) as conn:
        catalog_root = conn.execute(
            "SELECT * FROM task_local_file_roots WHERE root_id = ?", (ROOT_ID,)
        ).fetchone()
        link = conn.execute(
            "SELECT * FROM task_local_file_links WHERE link_id = ?", (PDF_LINK_ID,)
        ).fetchone()
    portable_text = repr((catalog_root, link))
    assert str(pdf.parent.parent.resolve()) not in portable_text
    assert "references/throwaway.pdf" in portable_text
    with sqlite3.connect(registry.root_bindings_path) as conn:
        local_root = conn.execute(
            "SELECT absolute_path FROM local_file_roots WHERE root_id = ?", (ROOT_ID,)
        ).fetchone()
    assert Path(local_root[0]) == pdf.parent.parent.resolve()


def test_registration_is_idempotent_without_replacing_created_at(tmp_path: Path) -> None:
    registry, pdf, _ = _registry(tmp_path)
    registry.register_root(root_id=ROOT_ID, root=pdf.parent.parent)
    existing = registry.get_document_link(
        store_id=STORE_ID,
        document_id=DOCUMENT_ID,
        link_id=PDF_LINK_ID,
    )
    replay = registry.register_link(
        link_id=PDF_LINK_ID,
        task_id=None,
        store_id=STORE_ID,
        document_id=DOCUMENT_ID,
        root_id=ROOT_ID,
        relative_path="references/throwaway.pdf",
        display_name="Reference PDF",
        suffix=".pdf",
        media_type="application/pdf",
        byte_length=pdf.stat().st_size,
        sha256=_sha256(pdf),
        sensitivity="ordinary",
        allowed_action="open",
        source_receipt_id="receipt-pdf",
    )
    assert replay == existing


def test_remote_metadata_is_inert_and_remote_activation_is_denied(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    client, actions, authority = _client(registry)
    metadata = client.get(
        f"/api/truth/doc/{DOCUMENT_ID}/local-files?store_id={STORE_ID}",
        headers={"Host": "localhost"},
        environ_base={"REMOTE_ADDR": "100.64.0.2"},
    )
    assert metadata.status_code == 200
    assert all(
        item["local_action_available"] is False
        for item in metadata.get_json()["links"]
    )
    activation = client.post(
        _url(PDF_LINK_ID),
        json={"link_id": PDF_LINK_ID, "action": "open"},
        headers=_headers(LOCAL_FILE_OPEN_INTENT),
        environ_base={"REMOTE_ADDR": "100.64.0.2"},
    )
    assert activation.status_code == 403
    assert actions.opened == []
    assert authority == []


def test_pdf_open_is_exact_authorized_shell_boundary(tmp_path: Path) -> None:
    registry, pdf, _ = _registry(tmp_path)
    client, actions, authority = _client(registry)
    body = {"link_id": PDF_LINK_ID, "action": "open"}
    response = client.post(
        _url(PDF_LINK_ID),
        json=body,
        headers=_headers(LOCAL_FILE_OPEN_INTENT),
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "link_id": PDF_LINK_ID,
        "action": "open",
        "status": "opened",
    }
    assert actions.opened == [pdf.resolve()]
    assert actions.revealed == []
    assert authority == [
        ("local_file.open", STORE_ID, DOCUMENT_ID, body),
    ]
    assert str(pdf) not in response.get_data(as_text=True)


def test_ppk_is_reveal_only_and_never_reaches_open(tmp_path: Path) -> None:
    registry, _, ppk = _registry(tmp_path)
    client, actions, authority = _client(registry)
    forbidden = client.post(
        _url(PPK_LINK_ID),
        json={"link_id": PPK_LINK_ID, "action": "open"},
        headers=_headers(LOCAL_FILE_OPEN_INTENT),
    )
    assert forbidden.status_code == 403
    assert actions.opened == []
    assert actions.revealed == []
    assert authority == []

    body = {"link_id": PPK_LINK_ID, "action": "reveal"}
    revealed = client.post(
        _url(PPK_LINK_ID),
        json=body,
        headers=_headers(LOCAL_FILE_REVEAL_INTENT),
    )
    assert revealed.status_code == 200
    assert actions.opened == []
    assert actions.revealed == [ppk.resolve()]
    assert authority == [("local_file.reveal", STORE_ID, DOCUMENT_ID, body)]


def test_intent_origin_membership_and_document_binding_fail_closed(
    tmp_path: Path,
) -> None:
    registry, _, _ = _registry(tmp_path)
    client, actions, authority = _client(registry)
    body = {"link_id": PDF_LINK_ID, "action": "open"}

    wrong_intent = client.post(
        _url(PDF_LINK_ID),
        json=body,
        headers=_headers(LOCAL_FILE_REVEAL_INTENT),
    )
    assert wrong_intent.status_code == 403
    wrong_origin = client.post(
        _url(PDF_LINK_ID),
        json=body,
        headers={
            PICKER_INTENT_HEADER: LOCAL_FILE_OPEN_INTENT,
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert wrong_origin.status_code == 403
    wrong_document = client.post(
        _url(PDF_LINK_ID, document_id=OTHER_DOCUMENT_ID),
        json=body,
        headers=_headers(LOCAL_FILE_OPEN_INTENT),
    )
    assert wrong_document.status_code == 404
    assert actions.opened == []
    assert authority == []


def test_size_or_hash_drift_is_visible_and_blocks_action(tmp_path: Path) -> None:
    registry, pdf, _ = _registry(tmp_path)
    client, actions, authority = _client(registry)
    # Keep the same byte length so the content hash check, not only size, is load-bearing.
    original = pdf.read_bytes()
    pdf.write_bytes(b"X" * len(original))

    metadata = client.get(
        f"/api/truth/doc/{DOCUMENT_ID}/local-files?store_id={STORE_ID}",
        headers={"Host": "localhost"},
    )
    statuses = {
        item["link_id"]: item["availability"]
        for item in metadata.get_json()["links"]
    }
    assert statuses[PDF_LINK_ID] == "changed"

    blocked = client.post(
        _url(PDF_LINK_ID),
        json={"link_id": PDF_LINK_ID, "action": "open"},
        headers=_headers(LOCAL_FILE_OPEN_INTENT),
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["error"]["code"] == "local_file_changed"
    assert actions.opened == []
    # Exact authority is consumed before the final race-resistant integrity check.
    assert len(authority) == 1


def test_non_active_catalog_root_status_fails_closed(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    link = registry.get_document_link(
        store_id=STORE_ID,
        document_id=DOCUMENT_ID,
        link_id=PDF_LINK_ID,
    )
    with sqlite3.connect(registry.catalog_path) as conn:
        conn.execute(
            "UPDATE task_local_file_roots SET status = 'frozen' WHERE root_id = ?",
            (ROOT_ID,),
        )
    with pytest.raises(LocalFileLinkError) as raised:
        registry.verified_path(link)
    assert raised.value.code == "local_file_root_unavailable"


def test_proxy_markers_deny_activation_even_from_loopback(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    client, actions, authority = _client(registry)
    response = client.post(
        _url(PDF_LINK_ID),
        json={"link_id": PDF_LINK_ID, "action": "open"},
        headers={**_headers(LOCAL_FILE_OPEN_INTENT), "X-Forwarded-For": "127.0.0.1"},
    )
    assert response.status_code == 403
    assert actions.opened == []
    assert authority == []


def test_default_windows_helper_uses_fixed_argv_without_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from work_buddy.cowork import local_files

    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(local_files.sys, "platform", "win32")
    monkeypatch.setattr(
        local_files.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append((list(argv), dict(kwargs))),
    )
    pdf = tmp_path / "throwaway.pdf"
    ppk = tmp_path / "throwaway.ppk"
    helper = DefaultLocalFileOsActions()
    helper.open_pdf(pdf)
    helper.reveal(ppk)
    assert calls[0][0] == ["explorer.exe", str(pdf)]
    assert calls[1][0] == ["explorer.exe", "/select,", str(ppk)]
    assert all(call[1]["shell"] is False for call in calls)
    with pytest.raises(LocalFileLinkError):
        helper.open_pdf(ppk)
