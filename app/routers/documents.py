"""Document endpoints — upload, list, review queue, four-eyes approval, erase."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.auth.principal import Principal, resolve_principal
from app.config import get_settings
from app.deps import get_pipeline, get_platform_store, get_store
from app.platform.scope import scoped_human_principal, selected_space_id
from app.schemas import DocumentSummary, PendingDocument
from app.security.policy import STATUS_APPROVED

router = APIRouter(prefix="/api", tags=["documents"])


def _space_scope(account_id: str, space_id: str, principal: Principal):
    scoped = scoped_human_principal(account_id, space_id, principal, get_platform_store())
    return scoped.account_id, selected_space_id(scoped), scoped.access_filter()


def _doc_summary(d: dict) -> DocumentSummary:
    return DocumentSummary(
        doc_id=d["doc_id"], title=d["title"], classification=d["classification"],
        location=d["location"], category=d["category"], chunks=d["chunks"],
        status=d.get("status", STATUS_APPROVED), pii_findings=d.get("pii_findings", 0),
        account_id=d.get("account_id", ""), space_id=d.get("space_id", ""),
    )


def _pending_out(d: dict) -> PendingDocument:
    return PendingDocument(
        doc_id=d["doc_id"], title=d["title"], classification=d["classification_label"],
        location=d["location"], category=d["category"], uploaded_by=d["uploaded_by"],
        has_pii=d["has_pii"], chunks=d["chunks"], account_id=d.get("account_id", ""),
        space_id=d.get("space_id", ""),
    )


@router.get("/documents", response_model=list[DocumentSummary])
def list_documents(
    account_id: str = "",
    space_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    _, _, access = _space_scope(account_id, space_id, principal)
    return [_doc_summary(d) for d in get_store().list_documents(access)]


@router.post("/upload", response_model=DocumentSummary)
async def upload(
    file: UploadFile = File(...),
    classification: str = Form("internal"),
    location: str = Form("global"),
    category: str = Form("general"),
    account_id: str = Form(""),
    space_id: str = Form(""),
    principal: Principal = Depends(resolve_principal),
):
    if not principal.is_employee:
        raise HTTPException(status_code=403, detail="Only employees can upload documents.")
    scoped_account, scoped_space, _ = _space_scope(account_id, space_id, principal)

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The file is empty.")

    settings = get_settings()
    try:
        result = get_pipeline().ingest_file(
            filename=file.filename or "upload.txt", data=data,
            classification=classification, location=location, category=category,
            uploaded_by=principal.user_id,
            tenant=principal.tenant_id,  # server-side — never a caller-supplied field
            require_approval=settings.require_approval,
            block_public_on_pii=settings.block_public_on_pii,
            pii_phase=settings.pii_phase,
            account_id=scoped_account,
            space_id=scoped_space,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return DocumentSummary(
        doc_id=result.doc_id, title=result.title, classification=result.classification,
        location=result.location, category=result.category, chunks=result.chunks,
        status=result.status, pii_findings=len(result.pii_findings),
        account_id=scoped_account, space_id=scoped_space,
    )


@router.get("/documents/pending", response_model=list[PendingDocument])
def list_pending(
    account_id: str = "",
    space_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    if not principal.is_employee:
        raise HTTPException(status_code=403, detail="Only employees can review documents.")
    _, _, access = _space_scope(account_id, space_id, principal)
    out = []
    for d in get_store().list_pending(principal.tenant_id):
        # Only surface pending docs this reviewer would be cleared to see once
        # approved — computed by the SAME AccessFilter, never a parallel rule.
        probe = {
            "tenant_id": principal.tenant_id, "classification": d["classification"],
            "location": d["location"], "category": d["category"], "status": STATUS_APPROVED,
            "account_id": d.get("account_id", ""), "space_id": d.get("space_id", ""),
        }
        if access.allows(probe):
            out.append(_pending_out(d))
    return out


@router.post("/documents/{doc_id}/approve")
def approve_document(
    doc_id: str,
    account_id: str = "",
    space_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    if not principal.is_employee:
        raise HTTPException(status_code=403, detail="Only employees can approve documents.")
    scoped_account, scoped_space, access = _space_scope(account_id, space_id, principal)

    # list_pending is tenant-scoped, so a doc from another tenant is simply not found.
    doc = next((d for d in get_store().list_pending(principal.tenant_id) if d["doc_id"] == doc_id), None)
    if not doc:
        raise HTTPException(status_code=404, detail="No pending document with that id.")
    if scoped_account and (doc.get("account_id") != scoped_account or doc.get("space_id") != scoped_space):
        raise HTTPException(status_code=404, detail="No pending document with that id in this space.")

    # Four-eyes: the approver must be a different person than the uploader.
    if doc["uploaded_by"] and doc["uploaded_by"] == principal.user_id:
        raise HTTPException(status_code=403, detail="You can't approve a document you uploaded (four-eyes rule).")

    # Clearance: the approver must be entitled to see what they publish.
    probe = {
        "tenant_id": principal.tenant_id, "classification": doc["classification"],
        "location": doc["location"], "category": doc["category"], "status": STATUS_APPROVED,
        "account_id": doc.get("account_id", ""), "space_id": doc.get("space_id", ""),
    }
    if not access.allows(probe):
        raise HTTPException(status_code=403, detail="You aren't cleared to approve a document at this classification.")

    changed = get_store().set_document_status(doc_id, STATUS_APPROVED, approved_by=principal.user_id)
    return {"approved": doc_id, "chunks": changed, "approved_by": principal.user_id}


@router.delete("/documents/{doc_id}")
def delete_document(
    doc_id: str,
    account_id: str = "",
    space_id: str = "",
    principal: Principal = Depends(resolve_principal),
):
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Only admin / DPO can erase documents.")
    scoped_account, scoped_space, _ = _space_scope(account_id, space_id, principal)
    meta = get_store().get_document_meta(doc_id)
    if not meta or meta.get("tenant_id") != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found.")
    if scoped_account and (meta.get("account_id") != scoped_account or meta.get("space_id") != scoped_space):
        raise HTTPException(status_code=404, detail="Document not found in this space.")
    removed = get_store().delete_document(doc_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"deleted": doc_id, "chunks_removed": removed}
