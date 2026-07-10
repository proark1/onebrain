"""Pydantic request/response models — the API contract."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: Optional[str] = None
    account_id: Optional[str] = Field(default=None, max_length=120)
    space_id: Optional[str] = Field(default=None, max_length=120)


class ConversationSummary(BaseModel):
    id: str
    title: str
    updated_at: str


class MessageOut(BaseModel):
    role: str
    content: str
    meta: dict = {}


class ConversationDetail(BaseModel):
    id: str
    title: str
    messages: list[MessageOut]


class DocumentSummary(BaseModel):
    doc_id: str
    title: str
    classification: str
    location: str
    category: str
    chunks: int
    status: str = "approved"
    pii_findings: int = 0
    account_id: str = ""
    space_id: str = ""


class JobStatusOut(BaseModel):
    id: str
    type: str
    status: str
    tenant_id: str
    account_id: str = ""
    space_id: str = ""
    result: Optional[dict] = None
    error: str = ""
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""


class PendingDocument(BaseModel):
    doc_id: str
    title: str
    classification: str
    location: str
    category: str
    uploaded_by: str
    has_pii: bool
    chunks: int
    account_id: str = ""
    space_id: str = ""


class RoleInfo(BaseModel):
    id: str
    label: str
    clearance: str
    scope: str


class SessionInfo(BaseModel):
    role_id: str
    role_label: str
    clearance: str
    location_label: str
    tenant_id: str = ""
    display_name: str = ""
    email: str = ""


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


# --- Service surface (non-human callers) ---------------------------------
class ServiceCaptureRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    title: Optional[str] = Field(default=None, max_length=200)
    account_id: Optional[str] = Field(default=None, max_length=120)
    space_id: Optional[str] = Field(default=None, max_length=120)
    app_id: Optional[str] = Field(default=None, max_length=80)
    purpose: Optional[str] = Field(default=None, max_length=80)


class ServiceAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    account_id: Optional[str] = Field(default=None, max_length=120)
    space_id: Optional[str] = Field(default=None, max_length=120)
    app_id: Optional[str] = Field(default=None, max_length=80)
    purpose: Optional[str] = Field(default=None, max_length=80)


class ServiceAskResponse(BaseModel):
    answer: str
    chunks_used: int = 0


class IntakeRecordOut(BaseModel):
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    app_id: str
    purpose: str
    source: str
    source_ref: str = ""
    record_type: str
    intent: str
    classification: str
    confidence: float
    status: str
    title: str
    summary: str
    extracted_facts: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    created_at: str = ""


class AssistantRecordOut(IntakeRecordOut):
    content: str = ""


class ServiceIntakeRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    title: Optional[str] = Field(default=None, max_length=200)
    source: str = Field(default="service", max_length=80)
    source_ref: str = Field(default="", max_length=200)
    record_type: str = Field(default="", max_length=80)
    intent: str = Field(default="", max_length=80)
    metadata: dict = Field(default_factory=dict)
    account_id: Optional[str] = Field(default=None, max_length=120)
    space_id: Optional[str] = Field(default=None, max_length=120)
    app_id: Optional[str] = Field(default=None, max_length=80)
    purpose: Optional[str] = Field(default=None, max_length=80)


class ServiceIntakeResponse(BaseModel):
    record: IntakeRecordOut


class AssistantRecordCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    title: Optional[str] = Field(default=None, max_length=200)
    record_type: str = Field(min_length=1, max_length=80)
    intent: str = Field(default="", max_length=80)
    source: str = Field(default="assistant", max_length=80)
    source_ref: str = Field(default="", max_length=200)
    purpose: str = Field(default="assistant_context", max_length=80)
    account_id: Optional[str] = Field(default=None, max_length=120)
    space_id: Optional[str] = Field(default=None, max_length=120)
    metadata: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)
    retention: dict = Field(default_factory=dict)


class AssistantRecordResponse(BaseModel):
    record: AssistantRecordOut


class AssistantRecordListResponse(BaseModel):
    records: list[AssistantRecordOut] = Field(default_factory=list)


class AssistantAuditEventCreate(BaseModel):
    action: str = Field(min_length=1, max_length=120)
    target_type: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=200)
    account_id: Optional[str] = Field(default=None, max_length=120)
    space_id: Optional[str] = Field(default=None, max_length=120)
    purpose: str = Field(default="assistant_action", max_length=80)
    decision: str = Field(default="recorded", max_length=80)
    metadata: dict = Field(default_factory=dict)


class AssistantAuditEventOut(BaseModel):
    id: str
    account_id: str
    actor_id: str
    actor_type: str
    action: str
    target_type: str
    target_id: str
    space_id: str = ""
    app_id: str = ""
    purpose: str = ""
    decision: str = ""
    meta: dict = Field(default_factory=dict)
    created_at: str = ""


class ServiceCapabilitiesResponse(BaseModel):
    tenant_id: str
    account_id: str = ""
    app_id: str = ""
    scopes: list[str] = Field(default_factory=list)
    space_ids: list[str] = Field(default_factory=list)
    purposes: list[str] = Field(default_factory=list)
    # Assistant contract vocabulary this deployment accepts, so callers can verify
    # write-compatibility up front instead of discovering drift via 422s.
    contract_version: str = ""
    record_types: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)


class BrandThemeOut(BaseModel):
    id: str
    account_id: str
    app_id: str = ""
    name: str = ""
    primary_color: str
    secondary_color: str
    accent_color: str
    background_color: str
    surface_color: str
    text_color: str
    muted_color: str
    success_color: str
    warning_color: str
    danger_color: str
    logo_url: str = ""
    source: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


class ServiceKeyCreate(BaseModel):
    scopes: list[str]
    label: Optional[str] = None
    app_id: Optional[str] = Field(default=None, max_length=80)
    space_ids: list[str] = Field(default_factory=list)
    purposes: list[str] = Field(default_factory=list)


class MintedKey(BaseModel):
    id: str
    key: str                    # the plaintext — shown ONCE, never retrievable again
    tenant_id: str
    scopes: list[str]
    label: str = ""
    account_id: str = ""
    app_id: str = ""
    space_ids: list[str] = Field(default_factory=list)
    purposes: list[str] = Field(default_factory=list)
    rotated_from_id: str = ""


class ServiceKeyInfo(BaseModel):
    id: str
    tenant_id: str
    scopes: list[str]
    label: str = ""
    account_id: str = ""
    app_id: str = ""
    space_ids: list[str] = Field(default_factory=list)
    purposes: list[str] = Field(default_factory=list)
    status: str = "active"
    last_used_at: str = ""
    last_used_endpoint: str = ""
    use_count: int = 0
    rotated_from_id: str = ""
    revoked_at: str = ""
