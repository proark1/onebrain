"""Legacy rollout callback namespace.

Hetzner rollouts converge through authenticated fleet reports, so this router
intentionally exposes no workflow callback endpoint.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/rollouts", tags=["rollouts"])
