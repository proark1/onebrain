"""Persistent, governed AI Employees module."""

from app.ai_employees.contracts import (
    AI_EMPLOYEES,
    AI_EMPLOYEES_APP_ID,
    AI_EMPLOYEES_CONTRACT_VERSION,
    get_ai_employee,
)

__all__ = [
    "AI_EMPLOYEES",
    "AI_EMPLOYEES_APP_ID",
    "AI_EMPLOYEES_CONTRACT_VERSION",
    "get_ai_employee",
]
