"""Pydantic request/response models — the API contract."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: Optional[str] = None


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


class PendingDocument(BaseModel):
    doc_id: str
    title: str
    classification: str
    location: str
    category: str
    uploaded_by: str
    has_pii: bool
    chunks: int


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
    display_name: str = ""
    email: str = ""


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


# --- Service surface (non-human callers) ---------------------------------
class ServiceCaptureRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    title: Optional[str] = None


class ServiceAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ServiceAskResponse(BaseModel):
    answer: str
    chunks_used: int = 0


class ServiceKeyCreate(BaseModel):
    scopes: list[str]
    label: Optional[str] = None


class MintedKey(BaseModel):
    id: str
    key: str                    # the plaintext — shown ONCE, never retrievable again
    tenant_id: str
    scopes: list[str]
    label: str = ""


class ServiceKeyInfo(BaseModel):
    id: str
    tenant_id: str
    scopes: list[str]
    label: str = ""
    status: str = "active"
