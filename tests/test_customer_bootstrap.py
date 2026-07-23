from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from app.accounting.base import accounting_category_id
from app.auth.passwords import hash_password
from app.platform.memory import MemoryPlatformStore
from app.provisioning.bundles import OPTIONAL_MODULE_IDS
from app.provisioning.customer_bootstrap import (
    CustomerBootstrapDescriptor,
    decode_customer_bootstrap,
    encode_customer_bootstrap,
    reconcile_customer_bootstrap,
)
from app.servicekeys.base import generate_key
from app.servicekeys.memory import MemoryServiceKeyStore
from app.sessions.base import Session
from app.sessions.memory import MemorySessionStore
from app.users.base import User
from app.users.memory import MemoryUserStore


def _descriptor() -> CustomerBootstrapDescriptor:
    return CustomerBootstrapDescriptor(
        account_id="onebrain-development",
        account_kind="project",
        customer_name="One Brain Development Gate",
        module_ids=OPTIONAL_MODULE_IDS,
    )


def _integration_keys() -> dict[str, str]:
    return {
        "assistant": generate_key()[2],
        "communication": generate_key()[2],
    }


def test_customer_bootstrap_descriptor_round_trips_and_is_deterministic():
    descriptor = _descriptor()

    encoded = encode_customer_bootstrap(descriptor)

    assert encoded == encode_customer_bootstrap(descriptor)
    assert decode_customer_bootstrap(encoded) == descriptor
    assert decode_customer_bootstrap("") is None


def test_customer_bootstrap_descriptor_stays_a_strict_five_field_set():
    # Already-released box images validate the descriptor field-for-field and reject
    # any extra key before they can reconcile. Adding a field would fail bootstrap on
    # a box provisioned onto an older, still-approved release, so the encoded shape
    # must stay exactly these five keys — per-account extras (e.g. the UI locale)
    # travel outside the descriptor (ONEBRAIN_CUSTOMER_DEFAULT_LOCALE).
    encoded = encode_customer_bootstrap(_descriptor())
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))

    assert set(payload) == {
        "schema_version",
        "account_id",
        "account_kind",
        "customer_name",
        "module_ids",
    }


def test_reconcile_applies_the_default_locale_passed_alongside_the_descriptor():
    # default_locale is a reconcile parameter (not a descriptor field): it lands on
    # the account, and an unsupported value is coerced to the German default rather
    # than crashing the box at bootstrap.
    def _reconcile(platform, locale):
        return reconcile_customer_bootstrap(
            replace(_descriptor(), module_ids=()),
            platform_store=platform,
            service_key_store=MemoryServiceKeyStore(),
            user_store=MemoryUserStore(),
            session_store=MemorySessionStore(),
            administrator_email="",
            integration_keys={},
            default_locale=locale,
        )

    english = MemoryPlatformStore()
    _reconcile(english, "en")
    assert english.get_account("onebrain-development").default_locale == "en"

    coerced = MemoryPlatformStore()
    _reconcile(coerced, "fr")
    assert coerced.get_account("onebrain-development").default_locale == "de"


@pytest.mark.parametrize(
    "encoded,match",
    [
        ("not-base64!", "base64"),
        ("e30", "fields"),
        ("eHh4", "JSON"),
        ("x" * 5000, "too large"),
    ],
)
def test_customer_bootstrap_descriptor_rejects_invalid_payloads(encoded: str, match: str):
    with pytest.raises(ValueError, match=match):
        decode_customer_bootstrap(encoded)


def test_customer_bootstrap_descriptor_rejects_unknown_module_and_unsafe_identity():
    with pytest.raises(ValueError, match="module ids"):
        encode_customer_bootstrap(replace(_descriptor(), module_ids=("not_a_module",)))

    with pytest.raises(ValueError, match="account id"):
        encode_customer_bootstrap(replace(_descriptor(), account_id="bad\naccount"))


def test_core_only_bootstrap_does_not_require_integration_keys():
    result = reconcile_customer_bootstrap(
        replace(_descriptor(), module_ids=()),
        platform_store=MemoryPlatformStore(),
        service_key_store=MemoryServiceKeyStore(),
        user_store=MemoryUserStore(),
        session_store=MemorySessionStore(),
        administrator_email="",
        integration_keys={},
    )

    assert result.spaces == 2
    assert result.apps == 1
    assert result.integration_keys == 0


def test_full_stack_bootstrap_creates_local_topology_credentials_and_audit_once():
    platform = MemoryPlatformStore()
    service_keys = MemoryServiceKeyStore()
    users = MemoryUserStore()
    sessions = MemorySessionStore()
    admin = users.create(User(
        id="usr_admin",
        email="owner@example.test",
        display_name="Administrator",
        password_hash=hash_password("correct horse battery staple"),
        tenant_id="onebrain-development",
        role_id="admin",
        location="all",
    ))
    raw_keys = _integration_keys()

    first = reconcile_customer_bootstrap(
        _descriptor(),
        platform_store=platform,
        service_key_store=service_keys,
        user_store=users,
        session_store=sessions,
        administrator_email=admin.email,
        integration_keys=raw_keys,
    )
    second = reconcile_customer_bootstrap(
        _descriptor(),
        platform_store=platform,
        service_key_store=service_keys,
        user_store=users,
        session_store=sessions,
        administrator_email=admin.email,
        integration_keys=raw_keys,
    )

    assert first.account_id == "onebrain-development"
    assert second.account_id == first.account_id
    assert len(platform.list_accounts()) == 1
    assert {space.kind for space in platform.list_spaces(first.account_id)} == {
        "personal", "business", "customer_service", "shared", "family",
    }
    installations = platform.list_app_installations(first.account_id)
    assert {installation.app_id for installation in installations} == {
        "onebrain_core", "assistant", "communication", "kpi_dashboard", "ai_employees", "buchhaltung",
    }

    # Buchhaltung seeds a deterministic Drive category group (+ owner membership)
    # wherever the module is enabled, so the malware-clean extraction trigger has a
    # category to recognise. Idempotent across both reconciles above.
    business = next(space for space in platform.list_spaces(first.account_id) if space.kind == "business")
    group_id = accounting_category_id(business.id)
    assert group_id in {group.id for group in platform.list_access_groups(first.account_id, business.id)}
    memberships = platform.list_access_group_memberships(first.account_id, admin.id)
    assert any(membership.group_id == group_id for membership in memberships)

    assert len(platform.list_audit(first.account_id)) == 1
    assert platform.list_audit(first.account_id)[0].action == "customer.bootstrap_reconciled"
    assert platform.get_brand_theme(first.account_id) is not None

    keys = service_keys.list_by_tenant(first.account_id)
    assert {key.app_id for key in keys} == {"assistant", "communication"}
    assistant = next(key for key in keys if key.app_id == "assistant")
    communication = next(key for key in keys if key.app_id == "communication")
    assert "assistant_context" in assistant.purposes
    assert not any(purpose.startswith("customer_service") for purpose in assistant.purposes)
    assert set(communication.purposes) == {"customer_service_answer", "customer_service_inbox"}
    assert {platform.get_space(space_id).kind for space_id in communication.space_ids} == {
        "customer_service", "shared",
    }


def test_bootstrap_repairs_only_configured_legacy_admin_and_revokes_its_sessions():
    platform = MemoryPlatformStore()
    service_keys = MemoryServiceKeyStore()
    users = MemoryUserStore()
    sessions = MemorySessionStore()
    password_hash = hash_password("correct horse battery staple")
    admin = users.create(User(
        id="usr_admin",
        email="owner@example.test",
        display_name="Administrator",
        password_hash=password_hash,
        tenant_id="nft_gym",
        role_id="admin",
        location="all",
        must_change_password=True,
    ))
    other = users.create(User(
        id="usr_other",
        email="other@example.test",
        display_name="Other admin",
        password_hash=password_hash,
        tenant_id="nft_gym",
        role_id="admin",
        location="all",
    ))
    sessions.create(Session(
        id="sess_admin",
        user_id=admin.id,
        tenant_id="nft_gym",
        created_at="2026-07-17T00:00:00+00:00",
        expires_at="2026-07-18T00:00:00+00:00",
    ))
    sessions.create(Session(
        id="sess_other",
        user_id=other.id,
        tenant_id="nft_gym",
        created_at="2026-07-17T00:00:00+00:00",
        expires_at="2026-07-18T00:00:00+00:00",
    ))

    result = reconcile_customer_bootstrap(
        _descriptor(),
        platform_store=platform,
        service_key_store=service_keys,
        user_store=users,
        session_store=sessions,
        administrator_email=admin.email,
        integration_keys=_integration_keys(),
    )

    repaired = users.get(admin.id)
    untouched = users.get(other.id)
    assert result.administrator_rebound is True
    assert repaired is not None
    assert repaired.tenant_id == "onebrain-development"
    assert repaired.password_hash == password_hash
    assert repaired.must_change_password is True
    assert untouched is not None and untouched.tenant_id == "nft_gym"
    assert sessions.get("sess_admin").revoked_at
    assert not sessions.get("sess_other").revoked_at


def test_full_stack_bootstrap_requires_distinct_assistant_and_communication_keys():
    shared = generate_key()[2]

    with pytest.raises(ValueError, match="distinct"):
        reconcile_customer_bootstrap(
            _descriptor(),
            platform_store=MemoryPlatformStore(),
            service_key_store=MemoryServiceKeyStore(),
            user_store=MemoryUserStore(),
            session_store=MemorySessionStore(),
            administrator_email="owner@example.test",
            integration_keys={"assistant": shared, "communication": shared},
        )


# --- RLS scoping on a box with no owner connection ---------------------------
# A customer box is deliberately denied ONEBRAIN_OPERATOR_DATABASE_URL, and
# pg_operator_database_url then falls back to the app DSN. `admin=True` silently
# became an unprivileged, unscoped connection, so every platform_accounts policy
# failed closed: the reconciler could not insert its own account (startup
# crash-looped) and list_accounts returned nothing (an empty Apps page).


class _FakeConn:
    def __init__(self, dsn):
        self.dsn = dsn


def _store(monkeypatch, *, dsn, operator_dsn, bootstrap_account_id=""):
    from app.platform import postgres as pg_module
    from app.platform.postgres import PostgresPlatformStore

    store = object.__new__(PostgresPlatformStore)
    store._psycopg = type("_Pg", (), {"connect": staticmethod(_FakeConn)})
    store._dsn = dsn
    store._operator_dsn = operator_dsn
    store._bootstrap_account_id = bootstrap_account_id

    scopes: list[dict] = []
    monkeypatch.setattr(
        pg_module,
        "set_rls_scope",
        lambda conn, **kw: scopes.append({"dsn": conn.dsn, **kw}),
    )
    return store, scopes


def test_bootstrap_account_write_is_scoped_to_its_own_account_without_an_owner_role(monkeypatch):
    store, scopes = _store(monkeypatch, dsn="app-dsn", operator_dsn="app-dsn")

    conn = store._conn(admin=True, account_id="onebrain_development_x")

    # It must not pretend to be privileged when no owner role exists, and must
    # scope to the row it writes — which the policy accepts as
    # `id = current_setting('app.account_id')`, and only for that one account.
    assert conn.dsn == "app-dsn"
    assert scopes == [{
        "dsn": "app-dsn",
        "tenant_id": "onebrain_development_x",
        "account_id": "onebrain_development_x",
        "space_id": "",
    }]


def test_upsert_bootstrap_account_asks_for_its_own_account_scope():
    """The regression itself: it requested `admin=True` and no scope at all.

    On a box with no owner role that resolves to an unprivileged, unscoped
    connection, and the INSERT fails the platform_accounts WITH CHECK with
    InsufficientPrivilege — which crash-looped onebrain-api at startup.
    """
    from app.platform.base import Account
    from app.platform.postgres import PostgresPlatformStore

    captured: dict = {}

    class _Cur:
        def execute(self, *a, **k):
            return None

        def fetchone(self):
            return ("acct_x", "organization", "Name", "usr_1", "active", None, "de")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    store = object.__new__(PostgresPlatformStore)
    store._conn = lambda **kw: captured.update(kw) or _Conn()

    store.upsert_bootstrap_account(
        Account(id="acct_x", kind="organization", name="Name", owner_user_id="usr_1")
    )

    assert captured == {"admin": True, "account_id": "acct_x"}


def test_admin_reads_fall_back_to_the_boxs_own_account_when_no_owner_role_exists(monkeypatch):
    store, scopes = _store(
        monkeypatch, dsn="app-dsn", operator_dsn="app-dsn",
        bootstrap_account_id="onebrain_development_x",
    )

    store._conn(admin=True)

    # Unscoped, RLS empties the result rather than erroring — silently.
    assert scopes and scopes[0]["account_id"] == "onebrain_development_x"


def test_a_box_without_a_descriptor_gains_no_implicit_account_scope(monkeypatch):
    store, scopes = _store(monkeypatch, dsn="app-dsn", operator_dsn="app-dsn")

    store._conn(admin=True)

    assert scopes == []


def test_mission_control_keeps_its_privileged_unscoped_admin_connection(monkeypatch):
    store, scopes = _store(
        monkeypatch, dsn="app-dsn", operator_dsn="owner-dsn",
        bootstrap_account_id="ignored",
    )

    conn = store._conn(admin=True)

    # MC bypasses RLS by role identity; narrowing it would break cross-account reads.
    assert conn.dsn == "owner-dsn"
    assert scopes == []
