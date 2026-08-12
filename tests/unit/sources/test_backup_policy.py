"""Executable guard for Sources' current backup threat-model decision."""

from work_buddy.backups.local import VITAL_DBS
from work_buddy.paths import RESOURCES


def test_sources_are_not_silently_added_to_unencrypted_vital_backup() -> None:
    """Source bytes require their authorized content-aware export contract.

    The existing vital snapshot may be uploaded as an unencrypted GitHub
    Release and cannot fence a restore against later redactions.  Registering
    the Sources root makes its ownership visible without opting its database or
    blobs into that pipeline.
    """

    assert RESOURCES["stores/sources"] == "db/sources"
    assert "stores/sources" not in VITAL_DBS.values()
    assert all(not resource.startswith("stores/sources") for resource in VITAL_DBS.values())
