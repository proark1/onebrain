from onebrain_sdk import OneBrainClient


def test_onebrain_client_sends_bearer_key_and_default_scope():
    calls = []

    def transport(method, url, headers, payload, timeout):
        calls.append({
            "method": method,
            "url": url,
            "headers": dict(headers),
            "payload": payload,
            "timeout": timeout,
        })
        return {"answer": "ok", "chunks_used": 2}

    client = OneBrainClient(
        "https://onebrain.example",
        "obk_test_secret",
        account_id="acme",
        space_id="sp_acme_service",
        app_id="communication",
        transport=transport,
    )

    response = client.ask("What are the support hours?")

    assert response["chunks_used"] == 2
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://onebrain.example/api/service/ask"
    assert calls[0]["headers"]["Authorization"] == "Bearer obk_test_secret"
    assert calls[0]["payload"] == {
        "question": "What are the support hours?",
        "account_id": "acme",
        "space_id": "sp_acme_service",
        "app_id": "communication",
    }


def test_onebrain_client_store_message_maps_to_intake_payload():
    calls = []

    def transport(method, url, headers, payload, timeout):
        calls.append(payload)
        return {"captured": "doc_1", "chunks": 1}

    client = OneBrainClient(
        "https://onebrain.example/",
        "obk_test_secret",
        account_id="acme",
        space_id="sp_service",
        app_id="communication",
        transport=transport,
    )

    result = client.store_message(
        channel="whatsapp",
        sender="+491234",
        external_id="wamid.1",
        text="I need help with my booking.",
        metadata={"language": "de"},
    )

    assert result["captured"] == "doc_1"
    assert calls[0]["content"] == "I need help with my booking."
    assert calls[0]["title"] == "whatsapp message from +491234"
    assert calls[0]["source"] == "communication"
    assert calls[0]["record_type"] == "message"
    assert calls[0]["purpose"] == "customer_service_inbox"
    assert calls[0]["metadata"]["channel"] == "whatsapp"
    assert calls[0]["metadata"]["external_id"] == "wamid.1"


def test_onebrain_client_capabilities_uses_get():
    calls = []

    def transport(method, url, headers, payload, timeout):
        calls.append((method, url, payload))
        return {"tenant_id": "acme", "scopes": ["read:public"]}

    client = OneBrainClient("https://onebrain.example", "obk_test_secret", transport=transport)

    assert client.capabilities()["tenant_id"] == "acme"
    assert calls == [("GET", "https://onebrain.example/api/service/capabilities", None)]


def test_onebrain_client_intake_uses_structured_endpoint():
    calls = []

    def transport(method, url, headers, payload, timeout):
        calls.append((method, url, payload))
        return {"record": {"id": "rec_1"}}

    client = OneBrainClient(
        "https://onebrain.example",
        "obk_test_secret",
        account_id="acme",
        app_id="assistant",
        transport=transport,
    )

    assert client.intake("Remember to prepare the proposal.", intent="task")["record"]["id"] == "rec_1"
    assert calls[0][0] == "POST"
    assert calls[0][1] == "https://onebrain.example/api/service/intake"
    assert calls[0][2]["content"] == "Remember to prepare the proposal."
    assert calls[0][2]["intent"] == "task"
    assert calls[0][2]["app_id"] == "assistant"
