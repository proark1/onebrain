"""Compatibility import for the first AI employee contract location.

The AI Employees feature is a standalone module. New code imports from
``app.ai_employees.contracts``; this module remains so existing integrations do
not break during the v1-to-v2 transition.
"""

from app.ai_employees.contracts import *  # noqa: F401,F403
