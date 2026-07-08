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


class ServiceCapabilitiesResponse(BaseModel):
    tenant_id: str
    account_id: str = ""
    app_id: str = ""
    scopes: list[str] = Field(default_factory=list)
    space_ids: list[str] = Field(default_factory=list)
    purposes: list[str] = Field(default_factory=list)


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
