from types import SimpleNamespace

import pytest

from app.intake.memory import MemoryIntakeStore
from app.intake.pipeline import IntakeInput, IntakePipeline


def _pipeline(*, pii_phase="dpia_signed", require_approval=False):
    settings = SimpleNamespace(pii_phase=pii_phase, require_approval=require_approval)
    store = MemoryIntakeStore()
    return IntakePipeline(store, settings), store


def test_intake_pipeline_classifies_extracts_and_stores_record():
    pipeline, store = _pipeline()

    record = pipeline.ingest(IntakeInput(
        tenant_id="acme",
        account_id="acme",
        space_id="sp_acme_service",
        app_id="communication",
        purpose="customer_service_inbox",
        source="communication",
        source_ref="wamid.1",
        content="Customer wants to reschedule booking on 2026-08-12 and asked about the price.",
        title="WhatsApp message",
        metadata={"channel": "whatsapp"},
    ))

    assert record.record_type == "message"
    assert record.intent == "booking"
    assert record.classification == "internal"
    assert record.status == "approved"
    assert record.extracted_facts["dates"] == ["2026-08-12"]
    assert {"booking", "sales_lead"} <= set(record.extracted_facts["signals"])
    assert store.get(record.id) == record


def test_intake_pipeline_blocks_pii_in_synthetic_phase():
    pipeline, _ = _pipeline(pii_phase="synthetic")

    with pytest.raises(ValueError, match="PII detected"):
        pipeline.ingest(IntakeInput(
            tenant_id="acme",
            account_id="acme",
            space_id="sp_acme_service",
            app_id="communication",
            purpose="customer_service_inbox",
            content="Please call me at +49 171 12345678.",
        ))


def test_intake_store_exports_and_deletes_by_space_scope():
    pipeline, store = _pipeline()
    service = pipeline.ingest(IntakeInput(
        tenant_id="acme",
        account_id="acme",
        space_id="sp_service",
        app_id="communication",
        purpose="customer_service_inbox",
        content="A customer asked for support.",
    ))
    personal = pipeline.ingest(IntakeInput(
        tenant_id="acme",
        account_id="acme",
        space_id="sp_personal",
        app_id="assistant",
        purpose="assistant_action",
        content="Private owner reminder.",
    ))

    exported = store.export_records("acme", account_id="acme", space_id="sp_service")
    assert [record["id"] for record in exported] == [service.id]

    assert store.delete_records_by_scope("acme", account_id="acme", space_id="sp_service") == 1
    assert store.get(service.id) is None
    assert store.get(personal.id) == personal
