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
