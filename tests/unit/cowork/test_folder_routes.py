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
    MAX_FOLDER_PATH_CHARS,
    PICKER_INTENT_HEADER,
    PICKER_INTENT_VALUE,
    create_folder_blueprint,
)
from work_buddy.cowork.native_folder_chooser import NativeFolderChooserError
from work_buddy.cowork.project_store import FolderLifecycleError, ProjectStoreManager
from work_buddy.truth.registry import TruthStoreRegistry


PICKER_HEADERS = {PICKER_INTENT_HEADER: PICKER_INTENT_VALUE}


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


def _client(
    tmp_path: Path,
    *,
    chooser=None,
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
        access_policy=FolderAccessPolicy((tmp_path,)),
        read_only=read_only,
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(blueprint)
    return app.test_client(), manager, registry


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
