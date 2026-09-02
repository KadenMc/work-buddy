from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path

import pytest

from work_buddy import cutover_maintenance as maintenance


def _safe_live_root() -> Path:
    return (Path.cwd() / ".data").resolve()


def _database(path: Path, payload: bytes = b"temporary-authority") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_exact_process_capability_is_required_and_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path / "rehearsal" / "projects.db")
    monkeypatch.setattr(
        maintenance,
        "_configured_live_authorities",
        lambda: (_safe_live_root(), ()),
    )
    authorization = maintenance.authorize_isolated_rehearsal_root(
        database.parent,
        authority_paths={"projects": database},
    )

    maintenance.require_isolated_rehearsal_path(
        database,
        domain="projects",
        authorization=authorization,
    )
    with pytest.raises(maintenance.CutoverMaintenanceError, match="required"):
        maintenance.require_isolated_rehearsal_path(
            database,
            domain="projects",
            authorization=None,
        )
    original_pin_check = maintenance._require_pinned_rehearsal_handles

    def pin_check_after_scope(
        authorization: maintenance.IsolatedRehearsalAuthorization,
        domain: str,
    ) -> None:
        if domain == "contracts":
            pytest.fail("pin validation ran before domain scope validation")
        return original_pin_check(authorization, domain)

    monkeypatch.setattr(
        maintenance,
        "_require_pinned_rehearsal_handles",
        pin_check_after_scope,
    )
    with pytest.raises(maintenance.CutoverMaintenanceError, match="scope"):
        maintenance.require_isolated_rehearsal_path(
            database,
            domain="contracts",
            authorization=authorization,
        )
    with pytest.raises(maintenance.CutoverMaintenanceError, match="changed"):
        maintenance.require_isolated_rehearsal_path(
            database,
            domain="projects",
            authorization=replace(authorization, proof="0" * 64),
        )


def test_one_capability_binds_all_four_exact_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "rehearsal"
    authorities = {
        "journal": _database(root / "journal.db", b"journal"),
        "projects": _database(root / "projects.db", b"projects"),
        "contracts": _database(root / "contracts.db", b"contracts"),
        "personal_knowledge": _database(root / "personal.db", b"personal"),
    }
    monkeypatch.setattr(
        maintenance,
        "_configured_live_authorities",
        lambda: (_safe_live_root(), ()),
    )
    authorization = maintenance.authorize_isolated_rehearsal_root(
        root,
        authority_paths=authorities,
    )

    for domain, database in authorities.items():
        maintenance.require_isolated_rehearsal_path(
            database,
            domain=domain,
            authorization=authorization,
        )


def test_capability_rejects_hard_link_to_any_live_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _database(tmp_path / "synthetic-live.db", b"do-not-open")
    rehearsal_root = tmp_path / "rehearsal"
    rehearsal_root.mkdir()
    alias = rehearsal_root / "projects.db"
    try:
        os.link(live, alias)
    except OSError as exc:  # pragma: no cover - host filesystem limitation
        pytest.skip(f"hard links unavailable: {exc}")
    monkeypatch.setattr(
        maintenance,
        "_configured_live_authorities",
        lambda: (_safe_live_root(), (live,)),
    )

    with pytest.raises(
        maintenance.CutoverMaintenanceError,
        match="configured authority database",
    ):
        maintenance.authorize_isolated_rehearsal_root(
            rehearsal_root,
            authority_paths={"projects": alias},
        )


def test_capability_rejects_file_replacement_after_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path / "rehearsal" / "contracts.db")
    monkeypatch.setattr(
        maintenance,
        "_configured_live_authorities",
        lambda: (_safe_live_root(), ()),
    )
    authorization = maintenance.authorize_isolated_rehearsal_root(
        database.parent,
        authority_paths={"contracts": database},
    )
    database.unlink()
    _database(database, b"replacement")

    with pytest.raises(maintenance.CutoverMaintenanceError, match="identity changed"):
        maintenance.require_isolated_rehearsal_path(
            database,
            domain="contracts",
            authorization=authorization,
        )


def test_capability_rejects_reparse_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    database = _database(actual / "journal.db")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows policy limitation
        pytest.skip(f"directory symlinks unavailable: {exc}")
    monkeypatch.setattr(
        maintenance,
        "_configured_live_authorities",
        lambda: (_safe_live_root(), ()),
    )

    with pytest.raises(maintenance.CutoverMaintenanceError, match="filesystem alias"):
        maintenance.authorize_isolated_rehearsal_root(
            alias,
            authority_paths={"journal": alias / database.name},
        )


def test_capability_rejects_configured_live_root_under_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rehearsal_root = tmp_path / "rehearsal"
    database = _database(rehearsal_root / "personal.db")
    configured_live_root = tmp_path / "configured-live"
    configured_live_root.mkdir()
    monkeypatch.setattr(
        maintenance,
        "_configured_live_authorities",
        lambda: (configured_live_root, ()),
    )

    with pytest.raises(maintenance.CutoverMaintenanceError, match="share a rehearsal root"):
        maintenance.authorize_isolated_rehearsal_root(
            rehearsal_root,
            authority_paths={"personal_knowledge": database},
        )
