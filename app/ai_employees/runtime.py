"""Persistent single-employee conversation runtime."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

from app.ai_employees.backends.base import AgentBackendRequest, BackendUnavailableError
from app.ai_employees.base import (
    AiAgentRun,
    AiEmployeeConversation,
    AiEmployeeMessage,
    now_iso,
)
from app.ai_employees.contracts import get_ai_employee
from app.ai_employees.prompting import compile_agent_messages
from app.ai_employees.memory_service import active_approved_memories
from app.security.policy import Classification


class AiEmployeeRuntime:
    def __init__(
        self,
        *,
        store,
        retrieval_service,
        backend_registry,
        max_output_tokens: int = 2_048,
    ):
        self.store = store
        self.retrieval_service = retrieval_service
        self.backend_registry = backend_registry
        self.max_output_tokens = max(1, min(int(max_output_tokens), 32_768))

    def create_conversation(
        self,
        *,
        principal,
        account_id: str,
        space_id: str,
        employee_id: str,
        title: str,
    ) -> AiEmployeeConversation:
        self._authorize_principal_scope(principal, account_id, space_id)
        profile = self.store.get_profile(
            employee_id,
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
        )
        if not profile:
            raise KeyError(f"AI employee is not configured: {employee_id}")
        if profile.status != "active":
            raise ValueError(f"AI employee is paused: {employee_id}")
        policy = self.store.get_model_policy(
            employee_id,
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
        )
        if not policy:
            raise ValueError(f"AI employee model policy is missing: {employee_id}")
        clean_title = " ".join((title or "").split())[:160] or f"Chat with {get_ai_employee(employee_id).name}"
        return self.store.save_conversation(AiEmployeeConversation(
            id=f"aic_{uuid4().hex}",
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
            employee_id=employee_id,
            human_owner_id=principal.user_id,
            title=clean_title,
            status="active",
            character_version_id=profile.default_version_id,
            model_policy_id=policy.id,
        ))

    def stream_turn(
        self,
        *,
        principal,
        conversation_id: str,
        question: str,
        idempotency_key: str,
    ):
        account_id, space_id = self._principal_scope(principal)
        conversation = self.store.get_conversation(
            conversation_id,
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
        )
        if not conversation:
            raise KeyError(f"AI employee conversation is not in scope: {conversation_id}")
        if conversation.human_owner_id != principal.user_id:
            raise PermissionError("Only the conversation owner can continue this AI employee chat.")
        if conversation.status != "active":
            raise ValueError("AI employee conversation is archived.")
        profile = self.store.get_profile(
            conversation.employee_id,
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
        )
        if not profile or profile.status != "active":
            raise ValueError("The selected AI employee is paused or unavailable.")
        question = (question or "").strip()
        if not question or len(question) > 8_000:
            raise ValueError("AI employee question must contain 1 to 8000 characters.")
        idempotency_key = (idempotency_key or "").strip()
        if not idempotency_key or len(idempotency_key) > 160:
            raise ValueError("AI employee turn idempotency key must contain 1 to 160 characters.")
        input_hash = self._input_hash(conversation, question)
        existing = self.store.get_run_by_idempotency(
            idempotency_key,
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
        )
        if existing:
            if existing.input_hash != input_hash or existing.conversation_id != conversation.id:
                raise ValueError("AI employee turn idempotency key conflicts with a different request.")
            yield from self._replay(conversation, existing)
            return

        policy = self.store.get_model_policy(
            conversation.employee_id,
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
        )
        version = self.store.get_character_version(
            conversation.character_version_id,
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
        )
        if not policy or not version:
            raise ValueError("AI employee configuration is incomplete.")

        hits = self.retrieval_service.retrieve(principal, question)
        classification = self._max_classification(hits)
        try:
            backend, model = self.backend_registry.resolve(policy, classification)
        except BackendUnavailableError:
            raise

        history = self.store.list_messages(
            conversation.id,
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
        )
        memories = active_approved_memories(self.store.list_memories(
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
            employee_id=conversation.employee_id,
            status="approved",
        ))
        messages = compile_agent_messages(
            employee=get_ai_employee(conversation.employee_id),
            character_payload=version.payload,
            question=question,
            conversation=[{"speaker": row.speaker_id, "content": row.content} for row in history],
            memories=memories,
            hits=hits,
            token_budget=max(1, int(policy.cost_limit_usd * 20_000)),
            cost_budget_usd=policy.cost_limit_usd,
        )
        run = self.store.save_run(AiAgentRun(
            id=f"air_{uuid4().hex}",
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
            conversation_id=conversation.id,
            mission_id="",
            employee_id=conversation.employee_id,
            backend=backend.provider,
            model=model,
            idempotency_key=idempotency_key,
            status="running",
            input_hash=input_hash,
            started_at=now_iso(),
        ))
        self.store.save_message(AiEmployeeMessage(
            id=f"aim_{uuid4().hex}",
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
            conversation_id=conversation.id,
            speaker_type="human",
            speaker_id=principal.user_id,
            visibility="shared",
            content=question,
            run_id=run.id,
        ))
        yield {
            "type": "run",
            "run_id": run.id,
            "employee_id": conversation.employee_id,
            "provider": backend.provider,
            "model": model,
            "replayed": False,
        }

        answer_parts: list[str] = []
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": None,
            "provider_session_ref": "",
        }
        warning = ""
        try:
            request = AgentBackendRequest(
                model=model,
                messages=messages,
                max_output_tokens=self.max_output_tokens,
            )
            for event in backend.stream(request):
                if event.type == "text":
                    answer_parts.append(event.text)
                    yield {"type": "text", "text": event.text}
                elif event.type == "usage":
                    usage = {
                        "prompt_tokens": event.prompt_tokens,
                        "completion_tokens": event.completion_tokens,
                        "cost_usd": event.cost_usd,
                        "provider_session_ref": event.provider_session_ref,
                    }
                elif event.type == "tool_request":
                    warning = "Tool requests are disabled in direct chat until a governed capability is bound."
                elif event.type == "warning":
                    warning = event.text[:500]
                elif event.type == "error":
                    raise RuntimeError("Normalized backend error")
        except Exception:
            self.store.save_run(replace(
                run,
                status="failed",
                warning=warning,
                error="AI employee backend failed.",
                completed_at=now_iso(),
            ))
            yield {
                "type": "error",
                "code": "backend_failed",
                "message": "The AI employee could not complete this turn.",
            }
            yield {"type": "done", "run_id": run.id, "replayed": False}
            return

        answer = "".join(answer_parts).strip()
        if not answer:
            answer = "I could not produce a safe response for this turn."
        citations = tuple(dict.fromkeys(hit.chunk.doc_id for hit in hits))
        self.store.save_message(AiEmployeeMessage(
            id=f"aim_{uuid4().hex}",
            tenant_id=principal.tenant_id,
            account_id=account_id,
            space_id=space_id,
            conversation_id=conversation.id,
            speaker_type="employee",
            speaker_id=conversation.employee_id,
            visibility="shared",
            content=answer,
            citations=citations,
            run_id=run.id,
        ))
        completed = self.store.save_run(replace(
            run,
            status="completed",
            provider_session_ref=str(usage["provider_session_ref"] or ""),
            prompt_tokens=int(usage["prompt_tokens"] or 0),
            completion_tokens=int(usage["completion_tokens"] or 0),
            cost_usd=float(usage["cost_usd"] or 0.0),
            warning=warning,
            completed_at=now_iso(),
        ))
        sources = self._sources(hits)
        yield {"type": "sources", "sources": sources}
        yield {
            "type": "usage",
            "prompt_tokens": completed.prompt_tokens,
            "completion_tokens": completed.completion_tokens,
            "cost_usd": completed.cost_usd,
            "provider_session_ref": completed.provider_session_ref,
        }
        yield {"type": "done", "run_id": run.id, "replayed": False}

    def _replay(self, conversation, run):
        messages = self.store.list_messages(
            conversation.id,
            tenant_id=conversation.tenant_id,
            account_id=conversation.account_id,
            space_id=conversation.space_id,
        )
        answer = next((
            row for row in messages
            if row.run_id == run.id and row.speaker_type == "employee"
        ), None)
        yield {
            "type": "run",
            "run_id": run.id,
            "employee_id": conversation.employee_id,
            "provider": run.backend,
            "model": run.model,
            "replayed": True,
        }
        if run.status == "completed" and answer:
            yield {"type": "text", "text": answer.content}
            yield {
                "type": "sources",
                "sources": [{"doc_id": doc_id} for doc_id in answer.citations],
            }
            yield {
                "type": "usage",
                "prompt_tokens": run.prompt_tokens,
                "completion_tokens": run.completion_tokens,
                "cost_usd": run.cost_usd,
                "provider_session_ref": run.provider_session_ref,
            }
        elif run.status == "failed":
            yield {
                "type": "error",
                "code": "backend_failed",
                "message": "The AI employee could not complete this turn.",
            }
        else:
            yield {
                "type": "error",
                "code": "turn_in_progress",
                "message": "This AI employee turn has not completed yet.",
            }
        yield {"type": "done", "run_id": run.id, "replayed": True}

    @staticmethod
    def _sources(hits) -> list[dict]:
        sources: dict[str, dict] = {}
        for hit in hits:
            source = sources.setdefault(hit.chunk.doc_id, {
                "doc_id": hit.chunk.doc_id,
                "title": hit.chunk.meta.get("doc_title", "Untitled"),
                "classification": hit.chunk.meta.get("classification_label", "internal"),
                "score": round(float(hit.score), 3),
            })
            source["score"] = max(source["score"], round(float(hit.score), 3))
        return list(sources.values())

    @staticmethod
    def _max_classification(hits) -> Classification:
        highest = Classification.PUBLIC
        for hit in hits:
            value = Classification.parse(hit.chunk.meta.get("classification", Classification.RESTRICTED))
            highest = max(highest, value)
        return highest

    @staticmethod
    def _input_hash(conversation, question: str) -> str:
        payload = {
            "conversation_id": conversation.id,
            "employee_id": conversation.employee_id,
            "character_version_id": conversation.character_version_id,
            "model_policy_id": conversation.model_policy_id,
            "question": question,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _authorize_principal_scope(principal, account_id: str, space_id: str) -> None:
        if principal.principal_type != "human":
            raise PermissionError("Human session required for AI employee conversations.")
        if principal.tenant_id != account_id:
            raise PermissionError("AI employee account is not in the human principal scope.")
        if principal.space_ids is not None and space_id not in principal.space_ids:
            raise PermissionError("AI employee space is not in the human principal scope.")

    @staticmethod
    def _principal_scope(principal) -> tuple[str, str]:
        if not principal.account_id or not principal.space_ids or len(principal.space_ids) != 1:
            raise PermissionError("AI employee runtime requires one explicit account and space.")
        space_id = next(iter(principal.space_ids))
        AiEmployeeRuntime._authorize_principal_scope(principal, principal.account_id, space_id)
        return principal.account_id, space_id
