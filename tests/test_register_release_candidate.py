from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.trust.release import verify_release_signature
from app.trust.signing import generate_keypair
from scripts.register_release_candidate import candidate_version, current_alembic_head, register_from_environment


class _Response:
    def __init__(self, body: dict):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def _env(private_key: str) -> dict[str, str]:
    digest = "sha256:" + "a" * 64
    return {
        "ONEBRAIN_MC_URL": "https://mc.onlyonebrain.com",
        "ONEBRAIN_RELEASE_CANDIDATE_KEY_ID": "candidate-ci",
        "ONEBRAIN_RELEASE_CANDIDATE_SECRET": "candidate-secret",
        "ONEBRAIN_DEV_RELEASE_PRIVATE_KEY": private_key,
        "GITHUB_SHA": "1" * 40,
        "GITHUB_RUN_NUMBER": "42",
        "ONEBRAIN_CANDIDATE_VERSION": "2026.07.13.42",
        "ONEBRAIN_ROLLBACK_KIND": "code_only",
        "ONEBRAIN_ONEBRAIN_API_DIGEST": digest,
        "ONEBRAIN_ONEBRAIN_WORKERS_DIGEST": digest,
        "ONEBRAIN_ONEBRAIN_ADMIN_UI_DIGEST": digest,
        "ONEBRAIN_ASSISTANT_IMAGE_REF": f"ghcr.io/proark1/assistant-service@{digest}",
        "ONEBRAIN_ASSISTANT_REVISION": "assistant-revision",
        "ONEBRAIN_COMMUNICATION_IMAGE_REF": f"ghcr.io/proark1/communication@{digest}",
        "ONEBRAIN_COMMUNICATION_REVISION": "communication-revision",
    }


def test_candidate_version_is_stable_for_a_run_number():
    now = datetime(2026, 7, 13, 23, 59, tzinfo=timezone.utc)
    assert candidate_version("42", now) == "2026.07.13.42"
    with pytest.raises(ValueError):
        candidate_version("bad", now)


def test_candidate_uses_the_single_current_alembic_head():
    assert current_alembic_head() == "0036_accounting_foundations"


def test_registration_prepares_then_dev_signs_exact_returned_manifest():
    private_key, public_key = generate_keypair()
    calls = []

    def opener(request, timeout):
        assert timeout == 30
        assert request.headers["Authorization"] == "Bearer candidate-secret"
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            payload = calls[0]
            return _Response({
                "release": {
                    **payload,
                    "status": "draft",
                    "created_at": "",
                    "security_notes": "",
                    "rollback_plan": "",
                    "signature": "",
                    "signing_key_id": "",
                    "promotion": None,
                },
                "manifest_digest": "digest",
            })
        return _Response({
            "release": {"promotion": {"state": "dev_pending"}},
            "manifest_digest": "digest",
            "created": True,
        })

    result = register_from_environment(_env(private_key), opener=opener)
    assert result["created"] is True
    assert [call["action"] for call in calls] == ["prepare", "register"]
    registered = calls[1]
    expected_modules = {
        "onebrain-api",
        "onebrain-workers",
        "onebrain-admin-ui",
        "assistant-service",
        "communication-api",
        "communication-widget",
        "communication-voice",
        "communication-workers",
    }
    assert set(calls[0]["modules"]) == expected_modules
    assert set(calls[0]["images"]) == expected_modules
    assert calls[0]["modules"]["assistant-service"] == "assistant-revision"
    assert calls[0]["modules"]["communication-voice"] == "communication-revision"
    assert calls[0]["images"]["communication-api"] == calls[0]["images"]["communication-workers"]
    fields = {
        key: registered[key]
        for key in (
            "version", "git_sha", "modules", "images", "migration_from",
            "migration_to", "rollback_kind",
        )
    }
    assert verify_release_signature(fields, registered["dev_signature"], public_key)


def test_registration_rejects_any_production_private_key_input():
    private_key, _ = generate_keypair()
    env = _env(private_key)
    env["ONEBRAIN_RELEASE_PRIVATE_KEY"] = "must-never-enter-ci"
    with pytest.raises(ValueError, match="forbidden"):
        register_from_environment(env, opener=lambda *_args, **_kwargs: None)


def test_registration_rejects_mutable_external_image_reference():
    private_key, _ = generate_keypair()
    env = _env(private_key)
    env["ONEBRAIN_ASSISTANT_IMAGE_REF"] = "ghcr.io/proark1/assistant-service:latest"
    with pytest.raises(ValueError, match="ONEBRAIN_ASSISTANT_IMAGE_REF is invalid"):
        register_from_environment(env, opener=lambda *_args, **_kwargs: None)
