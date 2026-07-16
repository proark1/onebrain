"""Postgres-backed AI Employees store with forced account/space RLS."""

from __future__ import annotations

import json
from dataclasses import asdict, fields, replace
from decimal import Decimal
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
    stable_record_id,
    validate_scope,
)
from app.ai_employees.contracts import (
    AI_EMPLOYEES,
    assert_no_raw_secrets,
    get_ai_employee,
    validate_ai_employee_provider,
)
from app.db.rls import set_rls_scope
from app.db.schema import validate_postgres_schema


_TABLES = {
    "versions": ("ai_employee_versions", AiEmployeeCharacterVersion),
    "profiles": ("ai_employee_profiles", AiEmployeeProfile),
    "model_policies": ("ai_employee_model_policies", AiEmployeeModelPolicy),
    "conversations": ("ai_employee_conversations", AiEmployeeConversation),
    "messages": ("ai_employee_messages", AiEmployeeMessage),
    "missions": ("ai_missions", AiMission),
    "participants": ("ai_mission_participants", AiMissionParticipant),
    "runs": ("ai_agent_runs", AiAgentRun),
    "memories": ("ai_employee_memories", AiEmployeeMemory),
    "connector_bindings": ("ai_connector_bindings", AiConnectorBinding),
    "action_proposals": ("ai_action_proposals", AiActionProposalRecord),
}

_JSON_FIELDS = {"payload", "task_overrides"}
_TUPLE_FIELDS = {
    "allowed_fallbacks",
    "citations",
    "source_refs",
    "resource_ids",
    "employee_ids",
    "capabilities",
    "source_record_ids",
}
_FLOAT_FIELDS = {"cost_limit_usd", "cost_budget_usd", "cost_usd"}
_TIMESTAMP_FIELDS = {
    "created_at",
    "updated_at",
    "published_at",
    "joined_at",
    "started_at",
    "completed_at",
    "retention_until",
    "approved_at",
    "expires_at",
}


def _from_json(key: str, row: dict):
    _, record_type = _TABLES[key]
    values = dict(row)
    for name in _TUPLE_FIELDS:
        if name in values:
            values[name] = tuple(values[name] or ())
    for name in _FLOAT_FIELDS:
        if name in values and isinstance(values[name], Decimal):
            values[name] = float(values[name])
    for name in _TIMESTAMP_FIELDS:
        if name in values and values[name] is None:
            values[name] = ""
    return record_type(**values)


class PostgresAiEmployeeStore:
    def __init__(self, dsn: str, operator_dsn: str | None = None):
        import psycopg

        self._psycopg = psycopg
        self._Jsonb = psycopg.types.json.Jsonb
        self._dsn = dsn
        self._operator_dsn = operator_dsn or dsn
        with self._conn() as conn:
            validate_postgres_schema(conn, tuple(table for table, _ in _TABLES.values()))

    def _conn(self, *, admin: bool = False):
        return self._psycopg.connect(self._operator_dsn if admin else self._dsn)

    @staticmethod
    def _scope(record) -> dict[str, str]:
        return {
            "tenant_id": record.tenant_id,
            "account_id": record.account_id,
            "space_id": record.space_id,
        }

    def _params(self, record) -> list:
        values = asdict(record)
        params: list = []
        for field in fields(record):
            value = values[field.name]
            if field.name in _JSON_FIELDS:
                value = self._Jsonb(value)
            elif field.name in _TUPLE_FIELDS:
                value = list(value)
            elif field.name in _TIMESTAMP_FIELDS and value == "":
                value = None
            params.append(value)
        return params

    def _upsert(self, key: str, record):
        validate_scope(**self._scope(record))
        table, _ = _TABLES[key]
        columns = [field.name for field in fields(record)]
        updates = [column for column in columns if column != "id"]
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
            "ON CONFLICT (id) DO UPDATE SET "
            + ", ".join(f"{column} = EXCLUDED.{column}" for column in updates)
        )
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(conn, **self._scope(record))
            cur.execute(sql, self._params(record))
            conn.commit()
        return record

    def _get(
        self, key: str, record_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ):
        table, _ = _TABLES[key]
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(conn, tenant_id=tenant_id, account_id=account_id, space_id=space_id)
            cur.execute(
                f"SELECT to_jsonb(row_data) FROM {table} row_data "
                "WHERE id = %s AND tenant_id = %s AND account_id = %s AND space_id = %s",
                (record_id, tenant_id, account_id, space_id),
            )
            row = cur.fetchone()
        return _from_json(key, row[0]) if row else None

    def _list(
        self, key: str, *, tenant_id: str, account_id: str, space_id: str = "",
    ) -> list:
        table, _ = _TABLES[key]
        clauses = ["tenant_id = %s", "account_id = %s"]
        params: list = [tenant_id, account_id]
        if space_id:
            clauses.append("space_id = %s")
            params.append(space_id)
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(conn, tenant_id=tenant_id, account_id=account_id, space_id=space_id)
            cur.execute(
                f"SELECT to_jsonb(row_data) FROM {table} row_data WHERE {' AND '.join(clauses)}",
                params,
            )
            rows = cur.fetchall()
        return [_from_json(key, row[0]) for row in rows]

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
        timestamp = now_iso()
        for employee in AI_EMPLOYEES:
            version_id = stable_record_id("aev", account_id, space_id, employee.id, "default-v1")
            profile_id = stable_record_id("aep", account_id, space_id, employee.id)
            policy_id = stable_record_id("aemodel", account_id, space_id, employee.id, "v1")
            if not self.get_character_version(
                version_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ):
                payload = default_character_payload(employee)
                self.save_character_version(AiEmployeeCharacterVersion(
                    id=version_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
                    employee_id=employee.id, version=1, state="published", payload=payload,
                    checksum=character_checksum(payload), author_id=author_id,
                    created_at=timestamp, published_at=timestamp,
                ))
            if not self.get_profile(
                employee.id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ):
                self.save_profile(AiEmployeeProfile(
                    id=profile_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
                    employee_id=employee.id, role=employee.role, department=employee.department,
                    pod=employee.pod, reports_to=employee.reports_to, status="active",
                    default_version_id=version_id, created_at=timestamp, updated_at=timestamp,
                ))
            if not self.get_model_policy(
                employee.id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ):
                self.save_model_policy(AiEmployeeModelPolicy(
                    id=policy_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
                    employee_id=employee.id, version=1, provider="gemini", model=default_model,
                    task_overrides={}, allowed_fallbacks=(), data_ceiling="internal",
                    cost_limit_usd=5.0, status="active", created_at=timestamp, updated_at=timestamp,
                ))
        return self.list_profiles(tenant_id=tenant_id, account_id=account_id, space_id=space_id)

    def save_profile(self, profile: AiEmployeeProfile) -> AiEmployeeProfile:
        get_ai_employee(profile.employee_id)
        if profile.status not in PROFILE_STATUSES:
            raise ValueError("Unknown AI employee profile status.")
        current = self._get("profiles", profile.id, **self._scope(profile))
        timestamp = now_iso()
        return self._upsert("profiles", replace(
            profile,
            created_at=(current.created_at if current else profile.created_at) or timestamp,
            updated_at=timestamp,
        ))

    def get_profile(
        self, employee_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiEmployeeProfile]:
        get_ai_employee(employee_id)
        return next((
            row for row in self._list(
                "profiles", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ) if row.employee_id == employee_id
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
        current = self._get("versions", version.id, **self._scope(version))
        if current and current.state == "published":
            if current == version:
                return current
            raise ValueError("Published AI employee character versions are immutable.")
        return self._upsert("versions", replace(
            version,
            checksum=character_checksum(version.payload),
            created_at=(current.created_at if current else version.created_at) or now_iso(),
        ))

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
        versions = self.list_character_versions(
            tenant_id=tenant_id, account_id=account_id, space_id=space_id, employee_id=employee_id,
        )
        version_number = max((row.version for row in versions), default=0) + 1
        return self.save_character_version(AiEmployeeCharacterVersion(
            id=stable_record_id("aev", account_id, space_id, employee_id, str(version_number), now_iso()),
            tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            employee_id=employee_id, version=version_number, state="draft", payload=dict(payload),
            checksum=character_checksum(payload), author_id=author_id, base_version_id=base_version_id,
        ))

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
        # Lock the profile and draft in one transaction so two admins cannot both
        # publish against the same current version.
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(conn, tenant_id=tenant_id, account_id=account_id, space_id=space_id)
            cur.execute(
                "SELECT to_jsonb(v) FROM ai_employee_versions v "
                "WHERE id = %s AND tenant_id = %s AND account_id = %s AND space_id = %s FOR UPDATE",
                (version_id, tenant_id, account_id, space_id),
            )
            version_row = cur.fetchone()
            if not version_row:
                raise KeyError(f"Unknown character version: {version_id}")
            version = _from_json("versions", version_row[0])
            if version.state != "draft":
                raise ValueError("Only a draft character version can be published.")
            cur.execute(
                "SELECT to_jsonb(p) FROM ai_employee_profiles p "
                "WHERE tenant_id = %s AND account_id = %s AND space_id = %s AND employee_id = %s FOR UPDATE",
                (tenant_id, account_id, space_id, version.employee_id),
            )
            profile_row = cur.fetchone()
            profile = _from_json("profiles", profile_row[0]) if profile_row else None
            if not profile or profile.default_version_id != expected_profile_version_id:
                raise ValueError("Character version conflict: the published profile changed.")
            timestamp = now_iso()
            cur.execute(
                "UPDATE ai_employee_versions SET state = 'published', published_at = %s "
                "WHERE id = %s",
                (timestamp, version.id),
            )
            cur.execute(
                "UPDATE ai_employee_profiles SET default_version_id = %s, updated_at = %s WHERE id = %s",
                (version.id, timestamp, profile.id),
            )
            conn.commit()
        return replace(version, state="published", published_at=timestamp)

    def get_character_version(
        self, version_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiEmployeeCharacterVersion]:
        return self._get(
            "versions", version_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        )

    def list_character_versions(
        self, *, tenant_id: str, account_id: str, space_id: str, employee_id: str = "",
    ) -> list[AiEmployeeCharacterVersion]:
        rows = self._list("versions", tenant_id=tenant_id, account_id=account_id, space_id=space_id)
        if employee_id:
            rows = [row for row in rows if row.employee_id == employee_id]
        return sorted(rows, key=lambda row: (row.employee_id, row.version, row.id))

    def save_model_policy(self, policy: AiEmployeeModelPolicy) -> AiEmployeeModelPolicy:
        get_ai_employee(policy.employee_id)
        validate_ai_employee_provider(policy.provider)
        if not policy.model.strip():
            raise ValueError("AI employee model is required.")
        assert_no_raw_secrets(policy.task_overrides, "model_policy")
        current = self._get("model_policies", policy.id, **self._scope(policy))
        timestamp = now_iso()
        return self._upsert("model_policies", replace(
            policy,
            created_at=(current.created_at if current else policy.created_at) or timestamp,
            updated_at=timestamp,
        ))

    def get_model_policy(
        self, employee_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiEmployeeModelPolicy]:
        rows = [row for row in self._list(
            "model_policies", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        ) if row.employee_id == employee_id and row.status == "active"]
        return max(rows, key=lambda row: (row.version, row.id), default=None)

    def list_model_policies(
        self, *, tenant_id: str, account_id: str, space_id: str,
    ) -> list[AiEmployeeModelPolicy]:
        return sorted(
            self._list("model_policies", tenant_id=tenant_id, account_id=account_id, space_id=space_id),
            key=lambda row: (row.employee_id, row.version, row.id),
        )

    def save_conversation(self, conversation: AiEmployeeConversation) -> AiEmployeeConversation:
        get_ai_employee(conversation.employee_id)
        if conversation.status not in CONVERSATION_STATUSES:
            raise ValueError("Unknown AI employee conversation status.")
        current = self._get("conversations", conversation.id, **self._scope(conversation))
        timestamp = now_iso()
        return self._upsert("conversations", replace(
            conversation,
            created_at=(current.created_at if current else conversation.created_at) or timestamp,
            updated_at=timestamp,
        ))

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
        current = self._get("messages", message.id, **self._scope(message))
        if current:
            if replace(message, created_at=current.created_at) == current:
                return current
            raise ValueError("AI employee messages are immutable.")
        return self._upsert("messages", replace(message, created_at=message.created_at or now_iso()))

    def list_messages(
        self, conversation_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> list[AiEmployeeMessage]:
        if not self.get_conversation(
            conversation_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        ):
            return []
        return sorted(
            [row for row in self._list(
                "messages", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ) if row.conversation_id == conversation_id],
            key=lambda row: (row.created_at, row.id),
        )

    def save_mission(self, mission: AiMission) -> AiMission:
        get_ai_employee(mission.accountable_employee_id)
        if mission.status not in MISSION_STATUSES:
            raise ValueError("Unknown AI employee mission status.")
        current = self._get("missions", mission.id, **self._scope(mission))
        timestamp = now_iso()
        return self._upsert("missions", replace(
            mission,
            created_at=(current.created_at if current else mission.created_at) or timestamp,
            updated_at=timestamp,
        ))

    def get_mission(
        self, mission_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiMission]:
        return self._get("missions", mission_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id)

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
        current = self._get("participants", participant.id, **self._scope(participant))
        return self._upsert("participants", replace(
            participant, joined_at=(current.joined_at if current else participant.joined_at) or now_iso(),
        ))

    def list_mission_participants(
        self, mission_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> list[AiMissionParticipant]:
        if not self.get_mission(mission_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id):
            return []
        return sorted(
            [row for row in self._list(
                "participants", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            ) if row.mission_id == mission_id], key=lambda row: (row.joined_at, row.id),
        )

    def save_run(self, run: AiAgentRun) -> AiAgentRun:
        get_ai_employee(run.employee_id)
        if run.status not in RUN_STATUSES:
            raise ValueError("Unknown AI employee run status.")
        duplicate = self.get_run_by_idempotency(run.idempotency_key, **self._scope(run))
        if duplicate and duplicate.id != run.id:
            if duplicate.input_hash == run.input_hash:
                return duplicate
            raise ValueError("AI employee run idempotency key conflicts with a different input.")
        current = self._get("runs", run.id, **self._scope(run))
        return self._upsert("runs", replace(
            run, created_at=(current.created_at if current else run.created_at) or now_iso(),
        ))

    def get_run(self, run_id: str, *, tenant_id: str, account_id: str, space_id: str) -> Optional[AiAgentRun]:
        return self._get("runs", run_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id)

    def get_run_by_idempotency(
        self, idempotency_key: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> Optional[AiAgentRun]:
        return next((row for row in self._list(
            "runs", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        ) if row.idempotency_key == idempotency_key), None)

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
        return self._upsert("memories", replace(
            memory,
            created_at=memory.created_at or now_iso(),
            approved_at=(memory.approved_at or now_iso()) if memory.status == "approved" else memory.approved_at,
        ))

    def list_memories(
        self, *, tenant_id: str, account_id: str, space_id: str, employee_id: str = "", status: str = "",
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
        current = self._get("connector_bindings", binding.id, **self._scope(binding))
        timestamp = now_iso()
        return self._upsert("connector_bindings", replace(
            binding,
            created_at=(current.created_at if current else binding.created_at) or timestamp,
            updated_at=timestamp,
        ))

    def list_connector_bindings(
        self, *, tenant_id: str, account_id: str, space_id: str,
    ) -> list[AiConnectorBinding]:
        return sorted(
            self._list("connector_bindings", tenant_id=tenant_id, account_id=account_id, space_id=space_id),
            key=lambda row: (row.updated_at, row.id), reverse=True,
        )

    def save_action_proposal(self, proposal: AiActionProposalRecord) -> AiActionProposalRecord:
        get_ai_employee(proposal.employee_id)
        assert_no_raw_secrets(proposal.payload, "action_payload")
        duplicate = self.get_action_proposal_by_idempotency(proposal.idempotency_key, **self._scope(proposal))
        if duplicate and duplicate.id != proposal.id:
            if duplicate.payload_hash == proposal.payload_hash:
                return duplicate
            raise ValueError("AI employee action idempotency key conflicts with a different payload.")
        current = self._get("action_proposals", proposal.id, **self._scope(proposal))
        timestamp = now_iso()
        return self._upsert("action_proposals", replace(
            proposal,
            created_at=(current.created_at if current else proposal.created_at) or timestamp,
            updated_at=timestamp,
        ))

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
        return next((row for row in self._list(
            "action_proposals", tenant_id=tenant_id, account_id=account_id, space_id=space_id,
        ) if row.idempotency_key == idempotency_key), None)

    def list_action_proposals(
        self, *, tenant_id: str, account_id: str, space_id: str, status: str = "",
    ) -> list[AiActionProposalRecord]:
        rows = self._list("action_proposals", tenant_id=tenant_id, account_id=account_id, space_id=space_id)
        if status:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: (row.updated_at, row.id), reverse=True)

    def export_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict:
        return {
            key: [asdict(row) for row in self._list(
                key, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
            )]
            for key in _TABLES
        }

    def delete_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(conn, tenant_id=tenant_id, account_id=account_id, space_id=space_id)
            for key in reversed(tuple(_TABLES)):
                table, _ = _TABLES[key]
                clauses = ["tenant_id = %s", "account_id = %s"]
                params: list = [tenant_id, account_id]
                if space_id:
                    clauses.append("space_id = %s")
                    params.append(space_id)
                cur.execute(f"DELETE FROM {table} WHERE {' AND '.join(clauses)}", params)
                counts[key] = cur.rowcount
            conn.commit()
        return counts
