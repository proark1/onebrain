"""Thread-safe JSON-backed store for local AI Employees development and tests."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, replace
from typing import Optional

from app.ai_employees.base import (
    CHARACTER_VERSION_STATES,
    CONNECTOR_STATUSES,
    CONVERSATION_STATUSES,
    MEMORY_STATUSES,
    MISSION_STATUSES,
    PROFILE_STATUSES,
    RUN_STATUSES,
    AiActionProposalRecord,
    AiAgentRun,
    AiConnectorBinding,
    AiEmployeeCharacterVersion,
    AiEmployeeConversation,
    AiEmployeeMemory,
    AiEmployeeMessage,
    AiEmployeeModelPolicy,
    AiEmployeeProfile,
    AiMission,
    AiMissionParticipant,
    character_checksum,
    default_character_payload,
    now_iso,
    scope_matches,
    stable_record_id,
    validate_scope,
)
from app.ai_employees.contracts import (
    AI_EMPLOYEES,
    assert_no_raw_secrets,
    get_ai_employee,
    validate_ai_employee_provider,
)


_TABLE_TYPES = {
    "profiles": AiEmployeeProfile,
    "versions": AiEmployeeCharacterVersion,
    "model_policies": AiEmployeeModelPolicy,
    "conversations": AiEmployeeConversation,
    "messages": AiEmployeeMessage,
    "missions": AiMission,
    "participants": AiMissionParticipant,
    "runs": AiAgentRun,
    "memories": AiEmployeeMemory,
    "connector_bindings": AiConnectorBinding,
    "action_proposals": AiActionProposalRecord,
}

_TUPLE_FIELDS = {
    "allowed_fallbacks",
    "citations",
    "source_refs",
    "resource_ids",
    "employee_ids",
    "capabilities",
    "source_record_ids",
}


def _record_from_dict(record_type, row: dict):
    values = dict(row)
    for field in _TUPLE_FIELDS:
        if field in values:
            values[field] = tuple(values[field] or ())
    return record_type(**values)


class MemoryAiEmployeeStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._tables: dict[str, dict[str, object]] = {name: {} for name in _TABLE_TYPES}
        self._persist_path = persist_path
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not (self._persist_path and os.path.exists(self._persist_path)):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._tables = {
                name: {
                    row["id"]: _record_from_dict(record_type, row)
                    for row in data.get(name, [])
                }
                for name, record_type in _TABLE_TYPES.items()
            }
        except Exception:
            self._tables = {name: {} for name in _TABLE_TYPES}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        temp_path = f"{self._persist_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    name: [asdict(record) for record in rows.values()]
                    for name, rows in self._tables.items()
                },
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        os.replace(temp_path, self._persist_path)

    @staticmethod
    def _scope(record) -> dict[str, str]:
        return {
            "tenant_id": record.tenant_id,
            "account_id": record.account_id,
            "space_id": record.space_id,
        }

    def _get(self, table: str, record_id: str, *, tenant_id: str, account_id: str, space_id: str):
        record = self._tables[table].get(record_id)
        return record if record and scope_matches(
            record, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        ) else None

    def _list(self, table: str, *, tenant_id: str, account_id: str, space_id: str = "") -> list:
        return [
            record for record in self._tables[table].values()
            if scope_matches(record, tenant_id=tenant_id, account_id=account_id, space_id=space_id)
        ]

    def _put(self, table: str, record):
        validate_scope(**self._scope(record))
        self._tables[table][record.id] = record
        self._save()
        return record

    def seed_defaults(
        self,
        *,
        tenant_id: str,
        account_id: str,
        space_id: str,
        author_id: str,
        default_model: str = "gemini/gemini-2.5-flash",
    ) -> list[AiEmployeeProfile]:
        validate_scope(tenant_id=tenant_id, account_id=account_id, space_id=space_id)
        if not (author_id or "").strip():
            raise ValueError("author_id is required to seed AI employees.")
        with self._lock:
            timestamp = now_iso()
            for employee in AI_EMPLOYEES:
                version_id = stable_record_id("aev", account_id, space_id, employee.id, "default-v1")
                profile_id = stable_record_id("aep", account_id, space_id, employee.id)
                policy_id = stable_record_id("aemodel", account_id, space_id, employee.id, "v1")

                if version_id not in self._tables["versions"]:
                    payload = default_character_payload(employee)
                    self._tables["versions"][version_id] = AiEmployeeCharacterVersion(
                        id=version_id,
                        tenant_id=tenant_id,
                        account_id=account_id,
                        space_id=space_id,
                        employee_id=employee.id,
                        version=1,
                        state="published",
                        payload=payload,
                        checksum=character_checksum(payload),
                        author_id=author_id,
                        created_at=timestamp,
                        published_at=timestamp,
                    )
                if profile_id not in self._tables["profiles"]:
                    self._tables["profiles"][profile_id] = AiEmployeeProfile(
                        id=profile_id,
                        tenant_id=tenant_id,
                        account_id=account_id,
                        space_id=space_id,
                        employee_id=employee.id,
                        role=employee.role,
                        department=employee.department,
                        pod=employee.pod,
                        reports_to=employee.reports_to,
                        status="active",
                        default_version_id=version_id,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                if policy_id not in self._tables["model_policies"]:
                    self._tables["model_policies"][policy_id] = AiEmployeeModelPolicy(
                        id=policy_id,
                        tenant_id=tenant_id,
                        account_id=account_id,
                        space_id=space_id,
                        employee_id=employee.id,
                        version=1,
                        provider="gemini",
                        model=default_model,
                        task_overrides={},
                        allowed_fallbacks=(),
                        data_ceiling="internal",
                        cost_limit_usd=5.0,
                        status="active",
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
            self._save()
            return self.list_profiles(tenant_id=tenant_id, account_id=account_id, space_id=space_id)

    def save_profile(self, profile: AiEmployeeProfile) -> AiEmployeeProfile:
        get_ai_employee(profile.employee_id)
        if profile.status not in PROFILE_STATUSES:
            raise ValueError("Unknown AI employee profile status.")
        with self._lock:
            current = self._tables["profiles"].get(profile.id)
            timestamp = now_iso()
            stored = replace(
                profile,
                created_at=(current.created_at if current else profile.created_at) or timestamp,
                updated_at=timestamp,
            )
            return self._put("profiles", stored)

    def get_profile(
        self, employee_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiEmployeeProfile]:
        get_ai_employee(employee_id)
        return next((
            profile for profile in self._list(
                "profiles", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ) if profile.employee_id == employee_id
        ), None)

    def list_profiles(self, *, tenant_id: str, account_id: str, space_id: str) -> list[AiEmployeeProfile]:
        rows = self._list("profiles", tenant_id=tenant_id, account_id=account_id, space_id=space_id)
        order = {employee.id: index for index, employee in enumerate(AI_EMPLOYEES)}
        return sorted(rows, key=lambda row: (order.get(row.employee_id, 999), row.id))

    def save_character_version(self, version: AiEmployeeCharacterVersion) -> AiEmployeeCharacterVersion:
        get_ai_employee(version.employee_id)
        if version.state not in CHARACTER_VERSION_STATES:
            raise ValueError("Unknown character version state.")
        assert_no_raw_secrets(version.payload, "character")
        with self._lock:
            current = self._tables["versions"].get(version.id)
            if current and current.state == "published":
                if current == version:
                    return current
                raise ValueError("Published AI employee character versions are immutable.")
            timestamp = now_iso()
            stored = replace(
                version,
                checksum=character_checksum(version.payload),
                created_at=(current.created_at if current else version.created_at) or timestamp,
            )
            return self._put("versions", stored)

    def create_character_draft(
        self,
        *,
        tenant_id: str,
        account_id: str,
        space_id: str,
        employee_id: str,
        payload: dict,
        author_id: str,
        base_version_id: str,
    ) -> AiEmployeeCharacterVersion:
        profile = self.get_profile(
            employee_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        )
        if not profile:
            raise KeyError(f"AI employee profile is not in scope: {employee_id}")
        if not self.get_character_version(
            base_version_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        ):
            raise ValueError("Character draft base version is not in scope.")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("Character payload must be a non-empty object.")
        if len(json.dumps(payload, ensure_ascii=False)) > 50_000:
            raise ValueError("Character payload exceeds the 50000 character limit.")
        assert_no_raw_secrets(payload, "character")
        existing = self.list_character_versions(
            tenant_id=tenant_id, account_id=account_id, space_id=space_id, employee_id=employee_id,
        )
        version_number = max((row.version for row in existing), default=0) + 1
        draft = AiEmployeeCharacterVersion(
            id=stable_record_id("aev", account_id, space_id, employee_id, str(version_number), now_iso()),
            tenant_id=tenant_id,
            account_id=account_id,
            space_id=space_id,
            employee_id=employee_id,
            version=version_number,
            state="draft",
            payload=dict(payload),
            checksum=character_checksum(payload),
            author_id=author_id,
            base_version_id=base_version_id,
        )
        return self.save_character_version(draft)

    def publish_character_version(
        self,
        version_id: str,
        *,
        tenant_id: str,
        account_id: str,
        space_id: str,
        actor_id: str,
        expected_profile_version_id: str,
    ) -> AiEmployeeCharacterVersion:
        with self._lock:
            version = self.get_character_version(
                version_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            )
            if not version:
                raise KeyError(f"Unknown character version: {version_id}")
            if version.state != "draft":
                raise ValueError("Only a draft character version can be published.")
            profile = self.get_profile(
                version.employee_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            )
            if not profile or profile.default_version_id != expected_profile_version_id:
                raise ValueError("Character version conflict: the published profile changed.")
            timestamp = now_iso()
            published = replace(version, state="published", published_at=timestamp)
            self._tables["versions"][published.id] = published
            self._tables["profiles"][profile.id] = replace(
                profile, default_version_id=published.id, updated_at=timestamp,
            )
            self._save()
            return published

    def get_character_version(
        self, version_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiEmployeeCharacterVersion]:
        return self._get(
            "versions", version_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        )

    def list_character_versions(
        self,
        *,
        tenant_id: str,
        account_id: str,
        space_id: str,
        employee_id: str = "",
    ) -> list[AiEmployeeCharacterVersion]:
        rows = self._list("versions", tenant_id=tenant_id, account_id=account_id, space_id=space_id)
        if employee_id:
            rows = [row for row in rows if row.employee_id == employee_id]
        return sorted(rows, key=lambda row: (row.employee_id, row.version, row.id))

    def save_model_policy(self, policy: AiEmployeeModelPolicy) -> AiEmployeeModelPolicy:
        get_ai_employee(policy.employee_id)
        validate_ai_employee_provider(policy.provider)
        if not (policy.model or "").strip():
            raise ValueError("AI employee model is required.")
        assert_no_raw_secrets(policy.task_overrides, "model_policy")
        with self._lock:
            current = self._tables["model_policies"].get(policy.id)
            timestamp = now_iso()
            stored = replace(
                policy,
                created_at=(current.created_at if current else policy.created_at) or timestamp,
                updated_at=timestamp,
            )
            return self._put("model_policies", stored)

    def get_model_policy(
        self, employee_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiEmployeeModelPolicy]:
        rows = [
            row for row in self._list(
                "model_policies", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ) if row.employee_id == employee_id and row.status == "active"
        ]
        return max(rows, key=lambda row: (row.version, row.id), default=None)

    def list_model_policies(
        self, *, tenant_id: str, account_id: str, space_id: str,
    ) -> list[AiEmployeeModelPolicy]:
        rows = self._list("model_policies", tenant_id=tenant_id, account_id=account_id, space_id=space_id)
        return sorted(rows, key=lambda row: (row.employee_id, row.version, row.id))

    def save_conversation(self, conversation: AiEmployeeConversation) -> AiEmployeeConversation:
        get_ai_employee(conversation.employee_id)
        if conversation.status not in CONVERSATION_STATUSES:
            raise ValueError("Unknown AI employee conversation status.")
        with self._lock:
            current = self._tables["conversations"].get(conversation.id)
            timestamp = now_iso()
            stored = replace(
                conversation,
                created_at=(current.created_at if current else conversation.created_at) or timestamp,
                updated_at=timestamp,
            )
            return self._put("conversations", stored)

    def get_conversation(
        self, conversation_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiEmployeeConversation]:
        return self._get(
            "conversations", conversation_id,
            tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        )

    def list_conversations(
        self, *, tenant_id: str, account_id: str, space_id: str, human_owner_id: str = "",
    ) -> list[AiEmployeeConversation]:
        rows = self._list("conversations", tenant_id=tenant_id, account_id=account_id, space_id=space_id)
        if human_owner_id:
            rows = [row for row in rows if row.human_owner_id == human_owner_id]
        return sorted(rows, key=lambda row: (row.updated_at, row.id), reverse=True)

    def save_message(self, message: AiEmployeeMessage) -> AiEmployeeMessage:
        if not self.get_conversation(
            message.conversation_id, tenant_id=message.tenant_id,
            account_id=message.account_id, space_id=message.space_id,
        ):
            raise ValueError("Message conversation is not in scope.")
        with self._lock:
            current = self._tables["messages"].get(message.id)
            if current:
                if current == message:
                    return current
                raise ValueError("AI employee messages are immutable.")
            stored = replace(message, created_at=message.created_at or now_iso())
            return self._put("messages", stored)

    def list_messages(
        self, conversation_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> list[AiEmployeeMessage]:
        if not self.get_conversation(
            conversation_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        ):
            return []
        rows = [
            row for row in self._list(
                "messages", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ) if row.conversation_id == conversation_id
        ]
        return sorted(rows, key=lambda row: (row.created_at, row.id))

    def save_mission(self, mission: AiMission) -> AiMission:
        get_ai_employee(mission.accountable_employee_id)
        if mission.status not in MISSION_STATUSES:
            raise ValueError("Unknown AI employee mission status.")
        with self._lock:
            current = self._tables["missions"].get(mission.id)
            timestamp = now_iso()
            stored = replace(
                mission,
                created_at=(current.created_at if current else mission.created_at) or timestamp,
                updated_at=timestamp,
            )
            return self._put("missions", stored)

    def get_mission(
        self, mission_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiMission]:
        return self._get(
            "missions", mission_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        )

    def list_missions(self, *, tenant_id: str, account_id: str, space_id: str) -> list[AiMission]:
        return sorted(
            self._list("missions", tenant_id=tenant_id, account_id=account_id, space_id=space_id),
            key=lambda row: (row.updated_at, row.id), reverse=True,
        )

    def save_mission_participant(self, participant: AiMissionParticipant) -> AiMissionParticipant:
        get_ai_employee(participant.employee_id)
        if not self.get_mission(
            participant.mission_id, tenant_id=participant.tenant_id,
            account_id=participant.account_id, space_id=participant.space_id,
        ):
            raise ValueError("Mission participant mission is not in scope.")
        with self._lock:
            current = self._tables["participants"].get(participant.id)
            stored = replace(participant, joined_at=(current.joined_at if current else participant.joined_at) or now_iso())
            return self._put("participants", stored)

    def list_mission_participants(
        self, mission_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> list[AiMissionParticipant]:
        if not self.get_mission(mission_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id):
            return []
        rows = [
            row for row in self._list(
                "participants", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ) if row.mission_id == mission_id
        ]
        return sorted(rows, key=lambda row: (row.joined_at, row.id))

    def save_run(self, run: AiAgentRun) -> AiAgentRun:
        get_ai_employee(run.employee_id)
        if run.status not in RUN_STATUSES:
            raise ValueError("Unknown AI employee run status.")
        with self._lock:
            duplicate = next((
                row for row in self._list(
                    "runs", tenant_id=run.tenant_id, account_id=run.account_id, space_id=run.space_id,
                ) if row.idempotency_key == run.idempotency_key and row.id != run.id
            ), None)
            if duplicate:
                if duplicate.input_hash == run.input_hash:
                    return duplicate
                raise ValueError("AI employee run idempotency key conflicts with a different input.")
            current = self._tables["runs"].get(run.id)
            stored = replace(run, created_at=(current.created_at if current else run.created_at) or now_iso())
            return self._put("runs", stored)

    def get_run(self, run_id: str, *, tenant_id: str, account_id: str, space_id: str) -> Optional[AiAgentRun]:
        return self._get("runs", run_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id)

    def get_run_by_idempotency(
        self, idempotency_key: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiAgentRun]:
        return next((
            row for row in self._list(
                "runs", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ) if row.idempotency_key == idempotency_key
        ), None)

    def list_runs(self, *, tenant_id: str, account_id: str, space_id: str) -> list[AiAgentRun]:
        return sorted(
            self._list("runs", tenant_id=tenant_id, account_id=account_id, space_id=space_id),
            key=lambda row: (row.created_at, row.id), reverse=True,
        )

    def save_memory(self, memory: AiEmployeeMemory) -> AiEmployeeMemory:
        get_ai_employee(memory.employee_id)
        if memory.status not in MEMORY_STATUSES:
            raise ValueError("Unknown AI employee memory status.")
        if not memory.source_refs:
            raise ValueError("AI employee memory requires source provenance.")
        if memory.status == "approved" and not memory.approved_by:
            raise ValueError("Approved AI employee memory requires a human approver.")
        stored = replace(
            memory,
            created_at=memory.created_at or now_iso(),
            approved_at=(memory.approved_at or now_iso()) if memory.status == "approved" else memory.approved_at,
        )
        with self._lock:
            return self._put("memories", stored)

    def list_memories(
        self, *, tenant_id: str, account_id: str, space_id: str, employee_id: str = "",
        status: str = "",
    ) -> list[AiEmployeeMemory]:
        rows = self._list("memories", tenant_id=tenant_id, account_id=account_id, space_id=space_id)
        if employee_id:
            rows = [row for row in rows if row.employee_id == employee_id]
        if status:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)

    def get_memory(
        self, memory_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiEmployeeMemory]:
        return self._get(
            "memories", memory_id,
            tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        )

    def save_connector_binding(self, binding: AiConnectorBinding) -> AiConnectorBinding:
        if binding.status not in CONNECTOR_STATUSES:
            raise ValueError("Unknown AI employee connector status.")
        if not binding.credential_ref.startswith("secret://"):
            raise ValueError("Connector binding requires an opaque secret reference.")
        for employee_id in binding.employee_ids:
            get_ai_employee(employee_id)
        with self._lock:
            current = self._tables["connector_bindings"].get(binding.id)
            timestamp = now_iso()
            stored = replace(
                binding,
                created_at=(current.created_at if current else binding.created_at) or timestamp,
                updated_at=timestamp,
            )
            return self._put("connector_bindings", stored)

    def list_connector_bindings(
        self, *, tenant_id: str, account_id: str, space_id: str,
    ) -> list[AiConnectorBinding]:
        return sorted(
            self._list(
                "connector_bindings", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ), key=lambda row: (row.updated_at, row.id), reverse=True,
        )

    def save_action_proposal(self, proposal: AiActionProposalRecord) -> AiActionProposalRecord:
        get_ai_employee(proposal.employee_id)
        assert_no_raw_secrets(proposal.payload, "action_payload")
        with self._lock:
            duplicate = next((
                row for row in self._list(
                    "action_proposals", tenant_id=proposal.tenant_id,
                    account_id=proposal.account_id, space_id=proposal.space_id,
                ) if row.idempotency_key == proposal.idempotency_key and row.id != proposal.id
            ), None)
            if duplicate:
                if duplicate.payload_hash == proposal.payload_hash:
                    return duplicate
                raise ValueError("AI employee action idempotency key conflicts with a different payload.")
            current = self._tables["action_proposals"].get(proposal.id)
            timestamp = now_iso()
            stored = replace(
                proposal,
                created_at=(current.created_at if current else proposal.created_at) or timestamp,
                updated_at=timestamp,
            )
            return self._put("action_proposals", stored)

    def get_action_proposal(
        self, proposal_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiActionProposalRecord]:
        return self._get(
            "action_proposals", proposal_id,
            tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        )

    def get_action_proposal_by_idempotency(
        self, idempotency_key: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiActionProposalRecord]:
        return next((
            row for row in self._list(
                "action_proposals", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ) if row.idempotency_key == idempotency_key
        ), None)

    def list_action_proposals(
        self, *, tenant_id: str, account_id: str, space_id: str, status: str = "",
    ) -> list[AiActionProposalRecord]:
        rows = self._list(
            "action_proposals", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        )
        if status:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: (row.updated_at, row.id), reverse=True)

    def export_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict:
        with self._lock:
            return {
                name: [asdict(row) for row in self._list(
                    name, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
                )]
                for name in _TABLE_TYPES
            }

    def delete_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for name in _TABLE_TYPES:
                ids = [
                    row.id for row in self._tables[name].values()
                    if scope_matches(row, tenant_id=tenant_id, account_id=account_id, space_id=space_id)
                ]
                for record_id in ids:
                    self._tables[name].pop(record_id, None)
                counts[name] = len(ids)
            self._save()
            return counts
