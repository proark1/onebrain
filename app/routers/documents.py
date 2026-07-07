"""Document endpoints — upload, list (permission-filtered), and erase."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.auth.principal import Principal, resolve_principal
from app.deps import get_pipeline, get_store
from app.schemas import DocumentSummary

router = APIRouter(prefix="/api", tags=["documents"])


@router.get("/documents", response_model=list[DocumentSummary])
def list_documents(principal: Principal = Depends(resolve_principal)):
    return get_store().list_documents(principal.access_filter())


@router.post("/upload", response_model=DocumentSummary)
async def upload(
    file: UploadFile = File(...),
    classification: str = Form("internal"),
    location: str = Form("global"),
    category: str = Form("general"),
    principal: Principal = Depends(resolve_principal),
):
    if not principal.is_employee:
        raise HTTPException(status_code=403, detail="Only employees can upload documents.")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The file is empty.")

    try:
        result = get_pipeline().ingest_file(
            filename=file.filename or "upload.txt", data=data,
            classification=classification, location=location, category=category,
            uploaded_by=principal.user_id,
            tenant=principal.tenant_id,  # server-side — never a caller-supplied field
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return DocumentSummary(
        doc_id=result.doc_id, title=result.title, classification=result.classification,
        location=result.location, category=result.category, chunks=result.chunks,
    )


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: str, principal: Principal = Depends(resolve_principal)):
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Only admin / DPO can erase documents.")
    removed = get_store().delete_document(doc_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"deleted": doc_id, "chunks_removed": removed}
