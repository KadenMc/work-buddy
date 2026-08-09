"""HTTP contract for host Folder discovery, inspection, setup, and open."""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from flask import Flask

from work_buddy.cowork.folder_api import (
    FolderAccessPolicy,
    FolderTokenStore,
    IMPORT_PICKER_INTENT_VALUE,
    LOCATION_PICKER_INTENT_VALUE,
    MARKDOWN_PICKER_INTENT_VALUE,
    MAX_FOLDER_PATH_CHARS,
    PICKER_INTENT_HEADER,
    PICKER_INTENT_VALUE,
    create_folder_blueprint,
)
from work_buddy.cowork.file_importers import (
    FileImporter,
    FileImporterRegistry,
    MARKDOWN_FILE_IMPORTER,
    MARKDOWN_MAX_SOURCE_BYTES,
)
from work_buddy.cowork.native_folder_chooser import NativeFolderChooserError
from work_buddy.cowork.project_store import FolderLifecycleError, ProjectStoreManager
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.registry import TruthStoreRegistry


PICKER_HEADERS = {PICKER_INTENT_HEADER: PICKER_INTENT_VALUE}
MARKDOWN_PICKER_HEADERS = {
    PICKER_INTENT_HEADER: MARKDOWN_PICKER_INTENT_VALUE
}
IMPORT_PICKER_HEADERS = {
    PICKER_INTENT_HEADER: IMPORT_PICKER_INTENT_VALUE
}
LOCATION_PICKER_HEADERS = {
    PICKER_INTENT_HEADER: LOCATION_PICKER_INTENT_VALUE
}


def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def _make_directory_redirect(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(link),
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            pytest.skip("Windows junction creation is unavailable")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")


def _remove_directory_redirect(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


def _windows_short_name(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    get_short_path_name = ctypes.windll.kernel32.GetShortPathNameW
    get_short_path_name.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    get_short_path_name.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(MAX_FOLDER_PATH_CHARS + 1)
    length = get_short_path_name(str(path), buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        pytest.skip("Windows short-name lookup is unavailable")
    return Path(buffer.value).name


def _client(
    tmp_path: Path,
    *,
    chooser=None,
    import_chooser=None,
    markdown_chooser=None,
    location_chooser=None,
    importer_registry=None,
    read_only=lambda: False,
    scan_budget: int = 2_000,
):
    manager = ProjectStoreManager(
        data_root=tmp_path / "machine",
        scan_budget=scan_budget,
        scan_hard_limit=100,
    )
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    blueprint = create_folder_blueprint(
        manager=manager,
        registry_factory=lambda: registry,
        chooser=chooser,
        import_chooser=import_chooser,
        markdown_chooser=markdown_chooser,
        location_chooser=location_chooser,
        **(
            {}
            if importer_registry is None
            else {"importer_registry": importer_registry}
        ),
        access_policy=FolderAccessPolicy((tmp_path,)),
        read_only=read_only,
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(blueprint)
    return app.test_client(), manager, registry


def _initialize_active_folder(
    tmp_path: Path,
    manager: ProjectStoreManager,
    registry: TruthStoreRegistry,
) -> tuple[Path, str]:
    folder = tmp_path / "active"
    folder.mkdir()
    inspection = manager.inspect(folder)
    store = manager.initialize(
        folder,
        registry=registry,
        inspection_fingerprint=inspection.fingerprint or "",
        idempotency_key="picker-active-folder",
    )
    return folder, store.store_id


def test_manual_inspect_then_explicit_initialize_and_list(tmp_path: Path) -> None:
    folder = tmp_path / "actual-folder-name"
    folder.mkdir()
    (folder / "draft.md").write_text("throwaway\n", encoding="utf-8")
    client, _, _ = _client(tmp_path)

    inspected = client.post(
        "/api/truth/cowork/folders/inspect",
        json={"folder_path": str(folder)},
    )
    assert inspected.status_code == 200
    payload = inspected.get_json()
    assert payload["status"] == "uninitialized"
    assert payload["folder_name"] == folder.name
    assert "store_id" not in payload
    assert "inspection_token" in payload
    assert not (folder / ".wbuddy").exists()

    initialized = client.post(
        "/api/truth/cowork/folders/initialize",
        json={
            "inspection_token": payload["inspection_token"],
            "idempotency_key": "route-setup",
        },
    )
    assert initialized.status_code == 200
    summary = initialized.get_json()["folder"]
    assert summary["folder_name"] == folder.name
    assert summary["layout"] == "wbuddy_cowork_v1"
    assert summary["permissions"]["create"] is True

    listed = client.get("/api/truth/cowork/folders").get_json()
    assert listed["folders"] == [summary]
    assert listed["chooser"]["available"] is False


def test_folder_list_revalidates_and_revives_a_recovered_store(
    tmp_path: Path,
) -> None:
    client, manager, registry = _client(tmp_path)
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    sidecar = folder / ".wbuddy" / "cowork"
    offline = tmp_path / "offline-cowork-sidecar"

    sidecar.rename(offline)
    unavailable = registry.get_by_path(folder)
    assert unavailable is not None
    assert unavailable.reachable is False
    offline.rename(sidecar)
    assert registry.list_stores(refresh=False)[0].reachable is False

    listed = client.get("/api/truth/cowork/folders")

    assert listed.status_code == 200
    payload = listed.get_json()
    assert [item["store_id"] for item in payload["folders"]] == [store_id]
    assert payload["folders"][0]["eligibility"] == "eligible"
    assert registry.list_stores(refresh=False)[0].reachable is True


def test_inspect_route_reports_redirected_managed_layout_without_writing_target(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "redirected-route"
    (folder / ".wbuddy").mkdir(parents=True)
    target = tmp_path / "outside-route-target"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_bytes(b"outside")
    link = folder / ".wbuddy" / "cowork"
    _make_directory_redirect(link, target)
    client, _, _ = _client(tmp_path)
    try:
        response = client.post(
            "/api/truth/cowork/folders/inspect",
            json={"folder_path": str(folder)},
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["status"] == "collision"
        assert payload["reason_code"] == "folder_layout_incomplete"
        assert sentinel.read_bytes() == b"outside"
        assert sorted(path.name for path in target.iterdir()) == [sentinel.name]
    finally:
        _remove_directory_redirect(link)


def test_host_chooser_is_honest_about_cancel_and_unavailability(tmp_path: Path) -> None:
    unavailable, _, _ = _client(tmp_path)
    response = unavailable.post(
        "/api/truth/cowork/folders/choose",
        headers=PICKER_HEADERS,
    )
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "folder_chooser_unavailable"

    cancelled, _, _ = _client(tmp_path / "cancel", chooser=lambda: None)
    response = cancelled.post(
        "/api/truth/cowork/folders/choose",
        headers=PICKER_HEADERS,
    )
    assert response.status_code == 200
    assert response.get_json() == {"cancelled": True, "ok": True}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {
            **PICKER_HEADERS,
            "Sec-Fetch-Site": "cross-site",
        },
        {
            **PICKER_HEADERS,
            "Origin": "https://unrelated.example",
        },
    ],
)
def test_host_chooser_requires_a_same_origin_dashboard_intent(
    tmp_path: Path,
    headers: dict[str, str],
) -> None:
    invoked = False

    def choose():
        nonlocal invoked
        invoked = True
        return tmp_path

    client, _, _ = _client(tmp_path, chooser=choose)
    response = client.post(
        "/api/truth/cowork/folders/choose",
        headers=headers,
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "folder_picker_intent_required"
    assert invoked is False


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("folder_chooser_busy", 409),
        ("folder_chooser_failed", 503),
    ],
)
def test_host_chooser_preserves_typed_failures(
    tmp_path: Path,
    code: str,
    status: int,
) -> None:
    def fail():
        raise NativeFolderChooserError(
            "The Folder picker could not be opened.",
            code=code,
            status=status,
            diagnostic="test-only diagnostic",
        )

    client, _, _ = _client(tmp_path, chooser=fail)
    response = client.post(
        "/api/truth/cowork/folders/choose",
        headers=PICKER_HEADERS,
    )
    payload = response.get_json()

    assert response.status_code == status
    assert payload["error"]["code"] == code
    assert payload["error"]["retryable"] is True
    if code == "folder_chooser_busy":
        assert "details" not in payload["error"]
    else:
        assert len(payload["error"]["details"]["trace_id"]) == 12
    assert "test-only diagnostic" not in str(payload)


def test_host_chooser_busy_is_not_logged_as_a_failure(
    tmp_path: Path,
    caplog,
) -> None:
    def fail():
        raise NativeFolderChooserError(
            "A Folder picker is already open.",
            code="folder_chooser_busy",
            status=409,
        )

    client, _, _ = _client(tmp_path, chooser=fail)
    response = client.post(
        "/api/truth/cowork/folders/choose",
        headers=PICKER_HEADERS,
    )

    assert response.status_code == 409
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_markdown_picker_returns_a_safe_folder_relative_path(
    tmp_path: Path,
) -> None:
    selected: dict[str, Path] = {}
    starts: list[Path] = []

    def choose(start):
        starts.append(Path(start))
        return selected["path"]

    client, manager, registry = _client(
        tmp_path,
        markdown_chooser=choose,
    )
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    markdown = folder / "research" / "資料.md"
    markdown.parent.mkdir()
    markdown.write_text("# Existing\n", encoding="utf-8")
    selected["path"] = markdown

    response = client.post(
        "/api/truth/cowork/files/choose-markdown",
        headers=MARKDOWN_PICKER_HEADERS,
        json={"store_id": store_id},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "cancelled": False,
        "path": "research/資料.md",
    }
    assert starts == [folder.resolve()]


@pytest.mark.parametrize("suffix", [".md", ".markdown", ".MD"])
def test_import_picker_returns_a_typed_markdown_importer(
    tmp_path: Path,
    suffix: str,
) -> None:
    selected: dict[str, Path] = {}
    starts: list[Path] = []

    def choose(start):
        starts.append(Path(start))
        return selected["path"]

    client, manager, registry = _client(
        tmp_path,
        import_chooser=choose,
    )
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    source = folder / "research" / f"paper{suffix}"
    source.parent.mkdir()
    source.write_text("# Existing\n", encoding="utf-8")
    source_sha256 = sha256_bytes(source.read_bytes())
    selected["path"] = source

    response = client.post(
        "/api/truth/cowork/files/choose-import",
        headers=IMPORT_PICKER_HEADERS,
        json={"store_id": store_id},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "cancelled": False,
        "path": f"research/paper{suffix}",
        "importer_id": "markdown/v1",
        "media_type": "text/markdown",
        "source_sha256": source_sha256,
        "importer": {
            "importer_id": "markdown/v1",
            "display_name": "Markdown",
            "source_format": "markdown",
            "media_type": "text/markdown",
            "suffixes": [".md", ".markdown"],
            "max_source_bytes": MARKDOWN_MAX_SOURCE_BYTES,
        },
    }
    assert starts == [folder.resolve()]


def test_import_picker_is_driven_by_an_injected_importer_registry(
    tmp_path: Path,
) -> None:
    selected: dict[str, Path] = {}
    synthetic = FileImporter(
        "fixture/v1",
        (".wbtest",),
        "application/x-wbtest",
        4096,
        display_name="Fixture document",
        source_format="fixture",
    )
    client, manager, registry = _client(
        tmp_path,
        import_chooser=lambda _start: selected["path"],
        importer_registry=FileImporterRegistry(
            (MARKDOWN_FILE_IMPORTER, synthetic)
        ),
    )
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    source = folder / "research" / "paper.wbtest"
    source.parent.mkdir()
    source.write_bytes(b"synthetic future-format source")
    selected["path"] = source

    response = client.post(
        "/api/truth/cowork/files/choose-import",
        headers=IMPORT_PICKER_HEADERS,
        json={"store_id": store_id},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "cancelled": False,
        "path": "research/paper.wbtest",
        "importer_id": "fixture/v1",
        "media_type": "application/x-wbtest",
        "source_sha256": sha256_bytes(source.read_bytes()),
        "importer": {
            "importer_id": "fixture/v1",
            "display_name": "Fixture document",
            "source_format": "fixture",
            "media_type": "application/x-wbtest",
            "suffixes": [".wbtest"],
            "max_source_bytes": 4096,
        },
    }


def test_import_picker_rejects_unsupported_files_without_changing_legacy_route(
    tmp_path: Path,
) -> None:
    selected: dict[str, Path] = {}
    chooser = lambda _start: selected["path"]
    client, manager, registry = _client(
        tmp_path,
        import_chooser=chooser,
        markdown_chooser=chooser,
    )
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    unsupported = folder / "paper.docx"
    unsupported.write_bytes(b"not-a-word-file")
    selected["path"] = unsupported

    generic = client.post(
        "/api/truth/cowork/files/choose-import",
        headers=IMPORT_PICKER_HEADERS,
        json={"store_id": store_id},
    )
    legacy = client.post(
        "/api/truth/cowork/files/choose-markdown",
        headers=MARKDOWN_PICKER_HEADERS,
        json={"store_id": store_id},
    )

    assert generic.status_code == 422
    assert generic.get_json()["error"]["code"] == "unsupported_file_type"
    assert legacy.status_code == 422
    assert legacy.get_json()["error"]["code"] == "invalid_markdown_file"


def test_import_picker_rejects_oversized_source_before_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: dict[str, Path] = {}
    client, manager, registry = _client(
        tmp_path,
        import_chooser=lambda _start: selected["path"],
    )
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    source = folder / "oversized.md"
    with source.open("wb") as stream:
        stream.truncate(MARKDOWN_MAX_SOURCE_BYTES + 1)
    selected["path"] = source

    def unexpected_hash(_payload: bytes) -> str:
        raise AssertionError("oversized picker source must not be hashed")

    monkeypatch.setattr(
        "work_buddy.cowork.folder_api.sha256_bytes",
        unexpected_hash,
    )

    response = client.post(
        "/api/truth/cowork/files/choose-import",
        headers=IMPORT_PICKER_HEADERS,
        json={"store_id": store_id},
    )

    assert response.status_code == 413
    assert response.get_json() == {
        "ok": False,
        "error": {
            "code": "import_source_too_large",
            "message": (
                "That file is too large to import. "
                f"The current limit is {MARKDOWN_MAX_SOURCE_BYTES} bytes."
            ),
            "retryable": False,
            "details": {
                "importer_id": "markdown/v1",
                "max_source_bytes": MARKDOWN_MAX_SOURCE_BYTES,
                "source_byte_length": MARKDOWN_MAX_SOURCE_BYTES + 1,
            },
        },
    }


@pytest.mark.parametrize(
    ("endpoint", "headers", "chooser_name"),
    [
        (
            "/api/truth/cowork/files/choose-import",
            IMPORT_PICKER_HEADERS,
            "import",
        ),
        (
            "/api/truth/cowork/files/choose-markdown",
            MARKDOWN_PICKER_HEADERS,
            "markdown",
        ),
        (
            "/api/truth/cowork/folders/choose-location",
            LOCATION_PICKER_HEADERS,
            "location",
        ),
    ],
)
def test_scoped_pickers_treat_cancel_as_a_normal_result(
    tmp_path: Path,
    endpoint: str,
    headers: dict[str, str],
    chooser_name: str,
) -> None:
    kwargs = {
        f"{chooser_name}_chooser": lambda _start: None,
    }
    client, manager, registry = _client(tmp_path, **kwargs)
    _, store_id = _initialize_active_folder(tmp_path, manager, registry)

    response = client.post(
        endpoint,
        headers=headers,
        json={"store_id": store_id},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "cancelled": True}


def test_location_picker_returns_root_and_nested_relative_locations(
    tmp_path: Path,
) -> None:
    selected: dict[str, Path] = {}

    def choose(_start):
        return selected["path"]

    client, manager, registry = _client(
        tmp_path,
        location_chooser=choose,
    )
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    nested = folder / "drafts" / "chapter"
    nested.mkdir(parents=True)

    selected["path"] = folder
    root_response = client.post(
        "/api/truth/cowork/folders/choose-location",
        headers=LOCATION_PICKER_HEADERS,
        json={"store_id": store_id},
    )
    selected["path"] = nested
    nested_response = client.post(
        "/api/truth/cowork/folders/choose-location",
        headers=LOCATION_PICKER_HEADERS,
        json={"store_id": store_id},
    )

    assert root_response.get_json() == {
        "ok": True,
        "cancelled": False,
        "path": "",
    }
    assert nested_response.get_json() == {
        "ok": True,
        "cancelled": False,
        "path": "drafts/chapter",
    }


@pytest.mark.parametrize(
    ("endpoint", "headers", "chooser_name"),
    [
        (
            "/api/truth/cowork/files/choose-import",
            IMPORT_PICKER_HEADERS,
            "import",
        ),
        (
            "/api/truth/cowork/files/choose-markdown",
            MARKDOWN_PICKER_HEADERS,
            "markdown",
        ),
        (
            "/api/truth/cowork/folders/choose-location",
            LOCATION_PICKER_HEADERS,
            "location",
        ),
    ],
)
@pytest.mark.parametrize(
    "request_change",
    [
        "missing-intent",
        "wrong-intent",
        "cross-site",
        "remote-peer",
        "proxied",
    ],
)
def test_scoped_pickers_require_direct_same_origin_browser_intent(
    tmp_path: Path,
    endpoint: str,
    headers: dict[str, str],
    chooser_name: str,
    request_change: str,
) -> None:
    invoked = False

    def choose(_start):
        nonlocal invoked
        invoked = True
        return tmp_path

    kwargs = {f"{chooser_name}_chooser": choose}
    client, manager, registry = _client(tmp_path, **kwargs)
    _, store_id = _initialize_active_folder(tmp_path, manager, registry)
    actual_headers = dict(headers)
    environ = {}
    if request_change == "missing-intent":
        actual_headers = {}
    elif request_change == "wrong-intent":
        actual_headers[PICKER_INTENT_HEADER] = PICKER_INTENT_VALUE
    elif request_change == "cross-site":
        actual_headers["Sec-Fetch-Site"] = "cross-site"
    elif request_change == "remote-peer":
        environ["REMOTE_ADDR"] = "100.64.0.42"
    else:
        actual_headers["X-Forwarded-For"] = "100.64.0.42"

    response = client.post(
        endpoint,
        headers=actual_headers,
        json={"store_id": store_id},
        environ_overrides=environ,
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "folder_picker_intent_required"
    assert invoked is False


@pytest.mark.parametrize(
    ("endpoint", "headers", "chooser_name"),
    [
        (
            "/api/truth/cowork/files/choose-import",
            IMPORT_PICKER_HEADERS,
            "import",
        ),
        (
            "/api/truth/cowork/files/choose-markdown",
            MARKDOWN_PICKER_HEADERS,
            "markdown",
        ),
        (
            "/api/truth/cowork/folders/choose-location",
            LOCATION_PICKER_HEADERS,
            "location",
        ),
    ],
)
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("folder_chooser_busy", 409),
        ("folder_chooser_timeout", 504),
        ("folder_chooser_failed", 503),
    ],
)
def test_scoped_pickers_preserve_process_level_failure_codes(
    tmp_path: Path,
    endpoint: str,
    headers: dict[str, str],
    chooser_name: str,
    code: str,
    status: int,
) -> None:
    def fail(_start):
        raise NativeFolderChooserError(
            "The picker could not be opened.",
            code=code,
            status=status,
            diagnostic="test-only scoped diagnostic",
        )

    kwargs = {f"{chooser_name}_chooser": fail}
    client, manager, registry = _client(tmp_path, **kwargs)
    _, store_id = _initialize_active_folder(tmp_path, manager, registry)

    response = client.post(
        endpoint,
        headers=headers,
        json={"store_id": store_id},
    )
    payload = response.get_json()

    assert response.status_code == status
    assert payload["error"]["code"] == code
    assert "test-only scoped diagnostic" not in str(payload)
    if code == "folder_chooser_busy":
        assert "details" not in payload["error"]
    else:
        assert len(payload["error"]["details"]["trace_id"]) == 12


@pytest.mark.parametrize(
    ("endpoint", "headers", "chooser_name"),
    [
        (
            "/api/truth/cowork/files/choose-import",
            IMPORT_PICKER_HEADERS,
            "import",
        ),
        (
            "/api/truth/cowork/files/choose-markdown",
            MARKDOWN_PICKER_HEADERS,
            "markdown",
        ),
        (
            "/api/truth/cowork/folders/choose-location",
            LOCATION_PICKER_HEADERS,
            "location",
        ),
    ],
)
def test_scoped_pickers_reject_unknown_store_without_opening_host_ui(
    tmp_path: Path,
    endpoint: str,
    headers: dict[str, str],
    chooser_name: str,
) -> None:
    invoked = False

    def choose(_start):
        nonlocal invoked
        invoked = True
        return tmp_path

    client, _, _ = _client(
        tmp_path,
        **{f"{chooser_name}_chooser": choose},
    )
    response = client.post(
        endpoint,
        headers=headers,
        json={"store_id": "ts_unknown"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "folder_unreachable"
    assert invoked is False


def test_markdown_picker_rejects_outside_managed_and_non_markdown_paths(
    tmp_path: Path,
) -> None:
    selected: dict[str, Path] = {}
    client, manager, registry = _client(
        tmp_path,
        markdown_chooser=lambda _start: selected["path"],
    )
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    managed = folder / ".wbuddy" / "hidden.md"
    managed.write_text("managed", encoding="utf-8")
    wrong_suffix = folder / "notes.txt"
    wrong_suffix.write_text("plain", encoding="utf-8")

    cases = [
        (outside, "markdown_outside_folder"),
        (managed, "invalid_markdown_file"),
        (wrong_suffix, "invalid_markdown_file"),
    ]
    for path, code in cases:
        selected["path"] = path
        response = client.post(
            "/api/truth/cowork/files/choose-markdown",
            headers=MARKDOWN_PICKER_HEADERS,
            json={"store_id": store_id},
        )
        assert response.status_code == 422
        assert response.get_json()["error"]["code"] == code


def test_markdown_picker_rejects_non_file_and_redirected_paths(
    tmp_path: Path,
) -> None:
    selected: dict[str, Path] = {}
    client, manager, registry = _client(
        tmp_path,
        markdown_chooser=lambda _start: selected["path"],
    )
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    directory_named_markdown = folder / "directory.md"
    directory_named_markdown.mkdir()
    target = tmp_path / "redirect-target"
    target.mkdir()
    (target / "linked.md").write_text("linked", encoding="utf-8")
    redirect = folder / "redirect"
    _make_directory_redirect(redirect, target)
    try:
        selected["path"] = directory_named_markdown
        non_file = client.post(
            "/api/truth/cowork/files/choose-markdown",
            headers=MARKDOWN_PICKER_HEADERS,
            json={"store_id": store_id},
        )
        selected["path"] = redirect / "linked.md"
        redirected = client.post(
            "/api/truth/cowork/files/choose-markdown",
            headers=MARKDOWN_PICKER_HEADERS,
            json={"store_id": store_id},
        )

        assert non_file.status_code == 409
        assert (
            non_file.get_json()["error"]["code"]
            == "markdown_file_unavailable"
        )
        assert redirected.status_code == 422
        assert redirected.get_json()["error"]["code"] == "invalid_markdown_file"
    finally:
        _remove_directory_redirect(redirect)


def test_markdown_picker_maps_selection_stat_failures_to_typed_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    selected: dict[str, Path] = {}
    client, manager, registry = _client(
        tmp_path,
        markdown_chooser=lambda _start: selected["path"],
    )
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    markdown = folder / "notes.md"
    markdown.write_text("notes", encoding="utf-8")
    selected["path"] = markdown

    def blocked(_path: Path) -> bool:
        raise PermissionError("blocked")

    monkeypatch.setattr(
        "work_buddy.cowork.folder_api._is_reparse_or_symlink",
        blocked,
    )

    response = client.post(
        "/api/truth/cowork/files/choose-markdown",
        headers=MARKDOWN_PICKER_HEADERS,
        json={"store_id": store_id},
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "markdown_file_unavailable"


def test_location_picker_rejects_outside_managed_file_and_redirect(
    tmp_path: Path,
) -> None:
    selected: dict[str, Path] = {}
    client, manager, registry = _client(
        tmp_path,
        location_chooser=lambda _start: selected["path"],
    )
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    outside = tmp_path / "outside-location"
    outside.mkdir()
    ordinary_file = folder / "not-a-location.md"
    ordinary_file.write_text("file", encoding="utf-8")
    target = tmp_path / "redirect-location-target"
    target.mkdir()
    redirect = folder / "redirect-location"
    _make_directory_redirect(redirect, target)
    try:
        cases = [
            (outside, "location_outside_folder", 422),
            (folder / ".wbuddy", "managed_location", 422),
            (ordinary_file, "location_unavailable", 409),
            (redirect, "location_unavailable", 422),
        ]
        for path, code, status in cases:
            selected["path"] = path
            response = client.post(
                "/api/truth/cowork/folders/choose-location",
                headers=LOCATION_PICKER_HEADERS,
                json={"store_id": store_id},
            )
            assert response.status_code == status
            assert response.get_json()["error"]["code"] == code
    finally:
        _remove_directory_redirect(redirect)


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 aliases are platform-specific")
def test_location_picker_rejects_managed_directory_via_windows_short_name(
    tmp_path: Path,
) -> None:
    selected: dict[str, Path] = {}
    client, manager, registry = _client(
        tmp_path,
        location_chooser=lambda _start: selected["path"],
    )
    folder, store_id = _initialize_active_folder(tmp_path, manager, registry)
    short_name = _windows_short_name(folder / ".wbuddy")
    if short_name.casefold() == ".wbuddy":
        pytest.skip("8.3 short names are disabled for this volume")
    selected["path"] = folder / short_name

    response = client.post(
        "/api/truth/cowork/folders/choose-location",
        headers=LOCATION_PICKER_HEADERS,
        json={"store_id": store_id},
    )

    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "managed_location"


@pytest.mark.parametrize(
    ("case", "path"),
    [
        ("nul", "C:\\Bad\u0000Folder"),
        ("oversized", "C:\\" + ("a" * MAX_FOLDER_PATH_CHARS)),
    ],
    ids=["nul", "oversized"],
)
def test_folder_access_policy_rejects_pathological_paths(
    case: str,
    path: str,
) -> None:
    del case
    with pytest.raises(FolderLifecycleError) as raised:
        FolderAccessPolicy().admit(path)

    assert raised.value.code == "invalid_path"
    assert raised.value.status == 400


def test_choose_returns_host_path_and_opaque_selection_token(tmp_path: Path) -> None:
    folder = tmp_path / "chosen"
    folder.mkdir()
    client, _, _ = _client(tmp_path, chooser=lambda: folder)

    chosen = client.post(
        "/api/truth/cowork/folders/choose",
        headers=PICKER_HEADERS,
    ).get_json()
    assert chosen["cancelled"] is False
    assert chosen["folder_path"] == str(folder.resolve())
    assert len(chosen["selection_token"]) == 32

    inspected = client.post(
        "/api/truth/cowork/folders/inspect",
        json={"selection_token": chosen["selection_token"]},
    ).get_json()
    assert inspected["status"] == "uninitialized"


def test_scan_continuation_token_hides_machine_scan_cursor(tmp_path: Path) -> None:
    folder = tmp_path / "wide"
    folder.mkdir()
    for index in range(4):
        (folder / f"child-{index}").mkdir()
    client, manager, _ = _client(tmp_path, scan_budget=1)

    response = client.post(
        "/api/truth/cowork/folders/inspect", json={"folder_path": str(folder)}
    ).get_json()
    assert response["status"] == "inspection_pending"
    public_token = response["continuation_token"]
    assert not (manager.scan_dir / f"{public_token}.json").exists()

    for _ in range(10):
        response = client.post(
            "/api/truth/cowork/folders/inspect",
            json={"continuation_token": response["continuation_token"]},
        ).get_json()
        if response["status"] != "inspection_pending":
            break
    assert response["status"] == "uninitialized"
    assert "inspection_token" in response


def test_nested_folder_boundary_details_survive_the_http_contract(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "parent"
    nested = folder / "workspace"
    nested.mkdir(parents=True)
    client, manager, registry = _client(tmp_path)
    nested_inspection = manager.inspect(nested)
    nested_store = manager.initialize(
        nested,
        registry=registry,
        inspection_fingerprint=nested_inspection.fingerprint or "",
        idempotency_key="nested-http-boundary",
    )

    response = client.post(
        "/api/truth/cowork/folders/inspect",
        json={"folder_path": str(folder)},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "contains_nested_folder"
    assert payload["boundaries"] == [
        {
            "folder_name": nested.name,
            "folder_path": str(nested.resolve()),
            "store_id": nested_store.store_id,
        }
    ]


def test_read_only_allows_inspection_but_blocks_setup(tmp_path: Path) -> None:
    folder = tmp_path / "readonly"
    folder.mkdir()
    client, _, _ = _client(tmp_path, read_only=lambda: True)
    inspected = client.post(
        "/api/truth/cowork/folders/inspect", json={"folder_path": str(folder)}
    ).get_json()
    assert inspected["status"] == "uninitialized"

    blocked = client.post(
        "/api/truth/cowork/folders/initialize",
        json={
            "inspection_token": inspected["inspection_token"],
            "idempotency_key": "blocked",
        },
    )
    assert blocked.status_code == 403
    assert blocked.get_json()["error"]["code"] == "dashboard_read_only"
    assert not (folder / ".wbuddy").exists()


def test_copied_initialized_folder_is_adopted_only_by_explicit_open(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "copied"
    folder.mkdir()
    seed_manager = ProjectStoreManager(data_root=tmp_path / "seed-machine")
    seed_registry = TruthStoreRegistry(tmp_path / "seed-registry.db")
    seed_inspection = seed_manager.inspect(folder)
    seeded = seed_manager.initialize(
        folder,
        registry=seed_registry,
        inspection_fingerprint=seed_inspection.fingerprint or "",
        idempotency_key="seed-copy",
    )
    before = _tree_bytes(folder)
    client, _, registry = _client(tmp_path)
    assert registry.list_stores(refresh=False) == ()

    inspected_response = client.post(
        "/api/truth/cowork/folders/inspect",
        json={"folder_path": str(folder)},
    )
    inspected = inspected_response.get_json()
    assert inspected_response.status_code == 200
    assert inspected["status"] == "initialized"
    assert inspected["store_id"] == seeded.store_id
    assert "inspection_token" in inspected
    assert registry.list_stores(refresh=False) == ()
    assert _tree_bytes(folder) == before

    opened = client.post(
        "/api/truth/cowork/folders/open",
        json={"inspection_token": inspected["inspection_token"]},
    )
    assert opened.status_code == 200
    assert opened.get_json()["folder"]["store_id"] == seeded.store_id
    assert registry.get_by_store_id(seeded.store_id, refresh=False) is not None
    assert _tree_bytes(folder) == before

    repeated = client.post(
        "/api/truth/cowork/folders/open",
        json={"inspection_token": inspected["inspection_token"]},
    )
    assert repeated.status_code == 200
    assert repeated.get_json()["folder"]["store_id"] == seeded.store_id
    assert _tree_bytes(folder) == before


def test_open_rejects_same_store_identity_at_another_live_folder(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first-copy"
    first.mkdir()
    manager = ProjectStoreManager(data_root=tmp_path / "machine")
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    initial = manager.inspect(first)
    original = manager.initialize(
        first,
        registry=registry,
        inspection_fingerprint=initial.fingerprint or "",
        idempotency_key="first-copy",
    )
    second = tmp_path / "second-copy"
    shutil.copytree(first, second)
    blueprint = create_folder_blueprint(
        manager=manager,
        registry_factory=lambda: registry,
        access_policy=FolderAccessPolicy((tmp_path,)),
        read_only=lambda: False,
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(blueprint)
    client = app.test_client()
    inspected = client.post(
        "/api/truth/cowork/folders/inspect",
        json={"folder_path": str(second)},
    ).get_json()
    assert inspected["status"] == "initialized"
    assert inspected["store_id"] == original.store_id

    collision = client.post(
        "/api/truth/cowork/folders/open",
        json={"inspection_token": inspected["inspection_token"]},
    )
    assert collision.status_code == 409
    assert collision.get_json()["error"]["code"] == "folder_store_collision"


def test_opaque_folder_tokens_prune_expired_host_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(
        "work_buddy.cowork.folder_api.time.time",
        lambda: clock["now"],
    )
    tokens = FolderTokenStore(tmp_path / "tokens", ttl_seconds=30)
    expired = tokens.issue(
        "selection",
        {"folder_path": str(tmp_path / "private")},
    )
    assert (tokens.root / f"{expired}.json").is_file()

    clock["now"] = 131.0
    current = tokens.issue(
        "selection",
        {"folder_path": str(tmp_path / "current")},
    )

    assert not (tokens.root / f"{expired}.json").exists()
    assert tokens.resolve(current, kind="selection")["folder_path"].endswith(
        "current"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_opaque_folder_tokens_use_private_posix_permissions(tmp_path: Path) -> None:
    tokens = FolderTokenStore(tmp_path / "tokens")
    token = tokens.issue("selection", {"folder_path": "/private/path"})

    assert stat.S_IMODE(tokens.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((tokens.root / f"{token}.json").stat().st_mode) == 0o600


def test_opaque_folder_tokens_prune_more_than_one_legacy_batch(
    tmp_path: Path,
) -> None:
    tokens = FolderTokenStore(tmp_path / "tokens")
    tokens.root.mkdir(parents=True)
    expired_body = json.dumps(
        {
            "kind": "selection",
            "expires_at": 0,
            "data": {"folder_path": "private"},
        }
    )
    for index in range(300):
        (tokens.root / f"{index:032x}.json").write_text(
            expired_body,
            encoding="utf-8",
        )

    current = tokens.issue("selection", {"folder_path": "current"})

    assert list(tokens.root.glob("*.json")) == [tokens.root / f"{current}.json"]
