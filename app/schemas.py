"""Pydantic request/response models — the API contract."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


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
