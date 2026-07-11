"""The load-bearing tests: prove the access boundary holds across roles."""

from __future__ import annotations

from tests.conftest import principal_for


def visible_titles(store, role_id, location="munich"):
    principal = principal_for(role_id, location)
    return {d["title"] for d in store.list_documents(principal.access_filter())}


def test_public_sees_only_public(store):
    titles = visible_titles(store, "public")
    assert "Opening hours & locations" in titles
    assert "Trainer salary bands 2026" not in titles
    assert "Q1 2026 revenue by location" not in titles
    assert "Refund & cancellation SOP" not in titles  # internal


def test_front_desk_cannot_see_hr_or_finance(store):
    titles = visible_titles(store, "front_desk")
    assert "Refund & cancellation SOP" in titles          # internal, own location
    assert "Trainer salary bands 2026" not in titles       # restricted / hr
    assert "Q1 2026 revenue by location" not in titles     # confidential / finance


def test_location_scoping_blocks_other_locations(store):
    munich = visible_titles(store, "front_desk", "munich")
    berlin = visible_titles(store, "front_desk", "berlin")
    assert "Munich front-desk opening checklist" in munich
    assert "Munich front-desk opening checklist" not in berlin
    assert "Berlin equipment maintenance log" in berlin
    assert "Berlin equipment maintenance log" not in munich


def test_hr_sees_hr_but_not_finance(store):
    titles = visible_titles(store, "hr")
    assert "Trainer salary bands 2026" in titles
    assert "Q1 2026 revenue by location" not in titles     # compartment: finance category


def test_finance_sees_finance_but_not_hr(store):
    titles = visible_titles(store, "finance")
    assert "Q1 2026 revenue by location" in titles
    assert "Trainer salary bands 2026" not in titles        # compartment: hr category


def test_admin_sees_everything(store):
    titles = visible_titles(store, "admin")
    assert "Trainer salary bands 2026" in titles
    assert "Q1 2026 revenue by location" in titles
    assert "Berlin equipment maintenance log" in titles


def test_pending_status_is_unreadable_even_by_admin():
    from app.security.policy import AccessFilter, Classification, STATUS_APPROVED, STATUS_PENDING

    admin = AccessFilter("nft_gym", int(Classification.RESTRICTED), None, None)
    base = {"tenant_id": "nft_gym", "classification": int(Classification.PUBLIC)}
    assert admin.allows({**base, "status": STATUS_APPROVED}) is True
    assert admin.allows({**base, "status": STATUS_PENDING}) is False   # parked, not live
    assert admin.allows(base) is True                                  # missing status = legacy/approved


def test_to_sql_enforces_approved_status():
    from app.security.policy import AccessFilter

    where, _ = AccessFilter("nft_gym", 3, None, None).to_sql()
    assert "status" in where and "approved" in where


def _personal_chunk(owner: str) -> dict:
    return {
        "tenant_id": "nft_gym",
        "classification": 0,                 # PUBLIC — clearance is not what gates this
        "space_id": "sp_personal",
        "space_kind": "personal",
        "owner_user_id": owner,
    }


def test_private_space_is_visible_only_to_its_owner():
    from app.security.policy import AccessFilter, Classification

    # Even a maximally-cleared admin cannot read a colleague's personal space.
    admin = AccessFilter("nft_gym", int(Classification.RESTRICTED), None, None, user_id="admin@nft_gym")
    assert admin.allows(_personal_chunk("alice@nft_gym")) is False

    alice = AccessFilter("nft_gym", int(Classification.INTERNAL), None, None, user_id="alice@nft_gym")
    assert alice.allows(_personal_chunk("alice@nft_gym")) is True   # her own space
    assert alice.allows(_personal_chunk("bob@nft_gym")) is False     # not hers


def test_private_space_denied_to_identityless_and_service_callers():
    from app.security.policy import AccessFilter, Classification

    # A caller with no user_id (service key path, unauthenticated) can never own a
    # private space, so a personal-space chunk with an empty owner is NOT world-readable.
    anon = AccessFilter("nft_gym", int(Classification.RESTRICTED), None, None)
    assert anon.allows(_personal_chunk("alice@nft_gym")) is False
    assert anon.allows(_personal_chunk("")) is False

    svc = AccessFilter("nft_gym", int(Classification.PUBLIC), frozenset(), frozenset({"general"}),
                       user_id="svc:comms-key")
    assert svc.allows(_personal_chunk("alice@nft_gym")) is False


def test_non_private_chunks_are_unaffected_by_owner_rule():
    from app.security.policy import AccessFilter, Classification

    # A chunk with no space_kind (the general corpus) is never treated as private,
    # regardless of who is asking — preserves pre-existing behaviour exactly.
    other = AccessFilter("nft_gym", int(Classification.INTERNAL), None, None, user_id="bob@nft_gym")
    shared = {"tenant_id": "nft_gym", "classification": int(Classification.INTERNAL)}
    assert other.allows(shared) is True
    assert other.allows({**shared, "space_kind": "business", "owner_user_id": "alice@nft_gym"}) is True


def test_to_sql_enforces_private_space_owner():
    from app.security.policy import AccessFilter, Classification

    where, params = AccessFilter(
        "nft_gym", int(Classification.RESTRICTED), None, None, user_id="alice@nft_gym"
    ).to_sql()
    assert "space_kind" in where and "owner_user_id" in where
    assert "alice@nft_gym" in params

    # With no identity, there is no owner escape hatch — private spaces are excluded outright.
    anon_where, anon_params = AccessFilter("nft_gym", 3, None, None).to_sql()
    assert "space_kind" in anon_where and "owner_user_id" not in anon_where
    assert all(p != "alice@nft_gym" for p in anon_params)
