"""Background job status endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException

from app.auth.principal import Principal, resolve_principal, service_principal_from_authorization
from app.deps import get_job_store, get_platform_store
from app.jobs.base import Job
from app.platform.scope import scoped_human_principal, selected_space_id
from app.schemas import JobStatusOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def job_status_out(job: Job) -> JobStatusOut:
    return JobStatusOut(
        id=job.id,
        type=job.type,
        status=job.status,
        tenant_id=job.tenant_id,
        account_id=job.account_id,
        space_id=job.space_id,
        result=job.result,
        error=job.error,
        attempts=job.attempts,
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
    )


def _can_read_job(job: Job, principal: Principal) -> bool:
    if job.tenant_id != principal.tenant_id:
        return False
    if principal.principal_type == "service":
        if job.requested_by == principal.user_id:
            return True
        if principal.account_id and job.account_id != principal.account_id:
            return False
        if principal.space_ids is not None and job.space_id not in principal.space_ids:
            return False
        return bool(job.account_id)
    if not job.account_id:
        return True
    try:
        scoped = scoped_human_principal(job.account_id, job.space_id, principal, get_platform_store())
    except HTTPException:
        return False
    return scoped.account_id == job.account_id and selected_space_id(scoped) == job.space_id


def resolve_job_principal(
    ob_session: str = Cookie(default=""),
    authorization: str = Header(default=""),
) -> Principal:
    if authorization.startswith("Bearer "):
        return service_principal_from_authorization(authorization, "jobs.read")
    return resolve_principal(ob_session=ob_session)


@router.get("/{job_id}", response_model=JobStatusOut)
def get_job(job_id: str, principal: Principal = Depends(resolve_job_principal)):
    job = get_job_store().get(job_id)
    if not job or not _can_read_job(job, principal):
        raise HTTPException(status_code=404, detail="Job not found.")
    return job_status_out(job)
