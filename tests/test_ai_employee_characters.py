"""Administrator character customization and version contracts."""

from __future__ import annotations

import pytest

from app.ai_employees.characters import (
    character_preview,
    create_character_draft,
    reset_character,
    rollback_character,
)
from app.ai_employees.memory import MemoryAiEmployeeStore


SCOPE = {"tenant_id": "acme", "account_id": "acme", "space_id": "business"}


def _store():
    store = MemoryAiEmployeeStore()
    store.seed_defaults(**SCOPE, author_id="system:test")
    return store


def test_character_draft_changes_style_without_changing_immutable_role_policy():
    store = _store()
    profile = store.get_profile("finance_manager", **SCOPE)
    draft = create_character_draft(
        store,
        **SCOPE,
        employee_id="finance_manager",
        patch={
            "display_name": "Sophie Laurent",
            "tone": "Short, calm, and numbers-first.",
            "character_prompt": "Start every report with cash exposure.",
        },
        author_id="admin",
    )
    assert draft.base_version_id == profile.default_version_id
    assert draft.payload["tone"] == "Short, calm, and numbers-first."
    assert "Finance Manager" in character_preview("finance_manager", draft.payload)
    assert "server-governed" in character_preview("finance_manager", draft.payload)
    assert store.get_profile("finance_manager", **SCOPE).role == "Finance Manager"

    with pytest.raises(ValueError, match="not editable"):
        create_character_draft(
            store, **SCOPE, employee_id="finance_manager",
            patch={"role": "Supreme Autonomous CFO"}, author_id="admin",
        )


def test_character_secret_scan_rejects_secret_fields_and_secret_values():
    store = _store()
    with pytest.raises(ValueError, match="Raw secret key"):
        create_character_draft(
            store, **SCOPE, employee_id="finance_manager",
            patch={"examples": [{"api_key": "raw"}]}, author_id="admin",
        )
    with pytest.raises(ValueError, match="Raw secret value"):
        create_character_draft(
            store, **SCOPE, employee_id="finance_manager",
            patch={"character_prompt": "Use sk-123456789012345678901234567890"}, author_id="admin",
        )


def test_character_rollback_and_reset_publish_new_versions_instead_of_mutating_history():
    store = _store()
    initial = store.get_profile("chief_of_staff", **SCOPE)
    customized = create_character_draft(
        store, **SCOPE, employee_id="chief_of_staff",
        patch={"display_name": "Clara H.", "tone": "Very terse."}, author_id="admin",
    )
    published = store.publish_character_version(
        customized.id, **SCOPE, actor_id="admin",
        expected_profile_version_id=initial.default_version_id,
    )
    assert published.version == 2

    rolled_back = rollback_character(
        store, **SCOPE, employee_id="chief_of_staff",
        source_version_id=initial.default_version_id, actor_id="admin",
        expected_profile_version_id=published.id,
    )
    assert rolled_back.version == 3
    assert rolled_back.payload["display_name"] == "Clara Hoffmann"

    customized_again = create_character_draft(
        store, **SCOPE, employee_id="chief_of_staff",
        patch={"display_name": "Different"}, author_id="admin",
    )
    published_again = store.publish_character_version(
        customized_again.id, **SCOPE, actor_id="admin",
        expected_profile_version_id=rolled_back.id,
    )
    reset = reset_character(
        store, **SCOPE, employee_id="chief_of_staff", actor_id="admin",
        expected_profile_version_id=published_again.id,
    )
    assert reset.version == 5
    assert reset.payload["display_name"] == "Clara Hoffmann"
    assert len(store.list_character_versions(**SCOPE, employee_id="chief_of_staff")) == 5
