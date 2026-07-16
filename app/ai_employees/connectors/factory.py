"""Composition helpers for the Google Calendar connector."""

from __future__ import annotations

import os

from app.ai_employees.connectors.google_calendar import GoogleCalendarConnector
from app.ai_employees.connectors.secrets import EncryptedFileConnectorSecretStore


def build_google_calendar_connector(settings, store):
    secret_store = None
    if settings.secret_encryption_key:
        path = (
            settings.ai_employees_connector_secret_store_path
            or os.path.join(settings.data_dir, "ai_employee_connector_secrets.json")
        )
        secret_store = EncryptedFileConnectorSecretStore(
            path=path,
            encryption_key=settings.secret_encryption_key,
        )
    return GoogleCalendarConnector(
        store=store,
        secret_store=secret_store,
        client_id=settings.ai_employees_google_client_id,
        client_secret=settings.ai_employees_google_client_secret,
        redirect_uri=settings.ai_employees_google_redirect_uri,
        state_signing_key=settings.auth_secret,
        timeout_seconds=settings.ai_employees_google_timeout_seconds,
        environment=settings.environment,
    )
