"""The Drive listing endpoint must carry the *requested* space's filing audience.

Regression guard for the stale-audience bug: the console loads the audience
(classifications, locations, departments) once from /bootstrap and then reloads
entries from /items when the user switches Drive roots. If /items omitted the
audience, the filing controls kept offering the previous space's departments and
a cross-space pick was rejected with a 422 (see _effective_policy). The default
(browse) listing now returns the target space's audience — that is the branch a
root switch always lands on, because select_root resets the view to "browse".
"""

from __future__ import annotations

from types import SimpleNamespace

from app.auth.principal import Principal
from app.drive.blobs import LocalDriveBlobStore
from app.drive.memory import MemoryDriveStore
from app.drive.service import DriveService
from app.jobs.memory import MemoryJobStore
from app.platform.base import AccessGroup, Account, Space
from app.platform.memory import MemoryPlatformStore
from app.routers import drive as drive_router
from app.security.policy import Classification
from app.store.memory import MemoryStore


ACCOUNT = "tenant_account"
SPACE_ALPHA = "space_alpha"
SPACE_BRAVO = "space_bravo"
OWNER = "user_owner"
ALPHA_DEPT = "acg_space_alpha_finance"
BRAVO_DEPT = "acg_space_bravo_buchhaltung"


def _principal() -> Principal:
    # categories=None => the caller can see every active department in a space,
    # so the audience reflects the space, not the caller's compartments.
    return Principal(
        user_id=OWNER,
        role_id="admin",
        role_label="Admin",
        clearance=Classification.RESTRICTED,
        locations=None,
        categories=None,
        location_label="all locations",
        tenant_id=ACCOUNT,
    )


def _service(tmp_path) -> DriveService:
    platform = MemoryPlatformStore()
    platform.create_account(Account(id=ACCOUNT, kind="organization", name="Acme", owner_user_id=OWNER))
    platform.create_space(Space(id=SPACE_ALPHA, account_id=ACCOUNT, kind="business", name="Alpha"))
    platform.create_space(Space(id=SPACE_BRAVO, account_id=ACCOUNT, kind="business", name="Bravo"))
    platform.upsert_access_group(AccessGroup(
        id=ALPHA_DEPT, account_id=ACCOUNT, space_id=SPACE_ALPHA, name="Finance",
    ))
    platform.upsert_access_group(AccessGroup(
        id=BRAVO_DEPT, account_id=ACCOUNT, space_id=SPACE_BRAVO, name="Buchhaltung",
    ))
    return DriveService(
        store=MemoryDriveStore(MemoryStore()),
        blobs=LocalDriveBlobStore(str(tmp_path / "drive"), min_free_bytes=0, min_free_percent=0),
        platform_store=platform,
        job_store=MemoryJobStore(),
        settings=SimpleNamespace(
            drive_private_spaces_enabled=False,
            drive_policy_mode="storage_and_indexing",
            job_max_attempts=3,
        ),
    )


def _department_ids(response: dict) -> set[str]:
    return {row["id"] for row in response["audience"]["departments"]}


def test_items_listing_returns_the_requested_space_departments(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(drive_router, "get_drive_service", lambda: service)

    alpha = drive_router.list_items(account_id=ACCOUNT, space_id=SPACE_ALPHA, principal=_principal())
    bravo = drive_router.list_items(account_id=ACCOUNT, space_id=SPACE_BRAVO, principal=_principal())

    assert _department_ids(alpha) == {"general", ALPHA_DEPT}
    assert _department_ids(bravo) == {"general", BRAVO_DEPT}
    # The whole point of the fix: another space's department must never leak into
    # this space's audience, or the console offers a category the backend will 422.
    assert BRAVO_DEPT not in _department_ids(alpha)
    assert ALPHA_DEPT not in _department_ids(bravo)


def test_items_review_listing_stays_minimal_without_an_audience(tmp_path, monkeypatch):
    # The review/legacy early-return branches are only reached by switching views
    # within a space (never on a root switch), so they deliberately omit the audience
    # and the console keeps the one it already holds. Guards against re-adding it and
    # perturbing the review response contract.
    service = _service(tmp_path)
    monkeypatch.setattr(drive_router, "get_drive_service", lambda: service)

    review = drive_router.list_items(
        account_id=ACCOUNT, space_id=SPACE_BRAVO, view="review", principal=_principal(),
    )

    assert "audience" not in review
