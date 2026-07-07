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
