"""Fleet alert webhook delivery (roadmap Gap D — the push channel).

The POST is exercised through an injected opener (no network), pinning the payload shape
and the never-raising, best-effort delivery contract.
"""

from __future__ import annotations

import json
import urllib.error

from app.fleet.alert_notify import format_alert_payload, push_alert, push_open_alerts
from app.fleet.base import FleetAlert


def _alert(kind="dev_pipeline_stalled", *, deployment_id="mc", detail="stuck 4h"):
    return FleetAlert(id=f"fa_{kind}", deployment_id=deployment_id, kind=kind,
                      detail=detail, status="open", created_at="2026-07-23T12:00:00+00:00")


class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    """Captures each request; returns a fake response, or raises for a chosen URL."""

    def __init__(self, status=200, raise_for=None):
        self.calls: list[dict] = []
        self._status = status
        self._raise_for = raise_for

    def __call__(self, request, timeout):
        self.calls.append({
            "url": request.full_url,
            "method": request.get_method(),
            "body": json.loads(request.data.decode("utf-8")),
            "timeout": timeout,
        })
        if self._raise_for is not None and request.full_url == self._raise_for:
            raise urllib.error.URLError("boom")
        return _Response(self._status)


def test_format_alert_payload_is_slack_compatible_and_structured():
    payload = format_alert_payload(_alert(detail="release 500 stuck"), source="mission-control")
    assert payload["text"] == "[mission-control] mc: dev_pipeline_stalled — release 500 stuck"
    assert payload["deployment_id"] == "mc"
    assert payload["kind"] == "dev_pipeline_stalled"
    assert payload["detail"] == "release 500 stuck"
    assert payload["source"] == "mission-control"


def test_push_alert_posts_json_and_returns_status():
    opener = _Opener(status=200)
    status = push_alert("https://hook.example/x", {"text": "hi"}, opener=opener)
    assert status == 200
    assert opener.calls[0]["url"] == "https://hook.example/x"
    assert opener.calls[0]["method"] == "POST"
    assert opener.calls[0]["body"] == {"text": "hi"}


def test_push_open_alerts_delivers_every_alert():
    opener = _Opener(status=200)
    pushed = push_open_alerts("https://hook.example/x", [_alert("low_root_disk"), _alert("unhealthy")],
                              opener=opener)
    assert pushed == 2
    assert {call["body"]["kind"] for call in opener.calls} == {"low_root_disk", "unhealthy"}


def test_push_open_alerts_skips_a_rejected_alert_without_raising():
    opener = _Opener(status=429)   # webhook rejects
    pushed = push_open_alerts("https://hook.example/x", [_alert()], opener=opener)
    assert pushed == 0             # counted as not delivered, but no exception


def test_push_open_alerts_never_raises_on_transport_failure():
    opener = _Opener(raise_for="https://hook.example/x")
    # Must swallow the URLError — a webhook outage can never break the watchdog tick.
    assert push_open_alerts("https://hook.example/x", [_alert()], opener=opener) == 0


def test_push_open_alerts_is_a_noop_without_a_url_or_alerts():
    opener = _Opener()
    assert push_open_alerts("", [_alert()], opener=opener) == 0
    assert push_open_alerts("https://hook.example/x", [], opener=opener) == 0
    assert opener.calls == []
