"""Push fleet alerts to an operator-configured webhook (roadmap Gap D — delivery).

The watchdog opens alerts (infra + pipeline) into the fleet-alert ledger; this delivers
the newly-opened ones to a webhook URL so a stall or a low disk actually reaches you
instead of waiting to be noticed in the console. One stdlib-``urllib`` POST per alert —
matching the fleet reporter, no new dependency, ``opener`` injectable for tests — and
NEVER raising: a webhook failure must never disturb the watchdog tick.

Dormant until ``fleet_alert_webhook_url`` is set, so landing this sends nothing. The
payload is metadata only (deployment id, kind, detail) — never a secret or customer
content — and carries a Slack-compatible ``text`` field plus structured fields for a
generic receiver.

Delivery is best-effort: an alert is pushed on the tick it first opens (a persistent alert
is never re-pushed), so a webhook outage at that moment means a missed push — the alert is
still in the console. A retrying/queued delivery is a later enhancement.
"""

from __future__ import annotations

import json
import logging
import urllib.request

_log = logging.getLogger("onebrain.fleet")


def format_alert_payload(alert, *, source: str = "mission-control") -> dict:
    """A generic + Slack-compatible webhook body for one alert (metadata only)."""
    return {
        "text": f"[{source}] {alert.deployment_id}: {alert.kind} — {alert.detail}",
        "source": source,
        "deployment_id": alert.deployment_id,
        "kind": alert.kind,
        "detail": alert.detail,
        "status": alert.status,
        "created_at": alert.created_at,
    }


def push_alert(url: str, payload: dict, *, opener=None, timeout: float = 10.0) -> int:
    """POST one payload; return the HTTP status. ``opener(request, timeout)`` is injectable
    so tests need no network."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"},
    )
    do_open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t))
    with do_open(request, timeout) as response:
        return getattr(response, "status", 0) or response.getcode()


def push_open_alerts(url: str, alerts, *, source: str = "mission-control",
                     opener=None, timeout: float = 10.0) -> int:
    """Deliver each alert to the webhook. Never raises; returns how many posted OK. One bad
    alert (a 4xx, or a webhook hiccup) is logged and skipped, never allowed to break the
    caller."""
    if not url or not alerts:
        return 0
    pushed = 0
    for alert in alerts:
        try:
            status = push_alert(url, format_alert_payload(alert, source=source),
                                opener=opener, timeout=timeout)
            if status >= 400:
                _log.warning("Alert webhook rejected %s/%s with HTTP %s",
                             alert.deployment_id, alert.kind, status)
            else:
                pushed += 1
        except Exception as exc:  # a webhook failure must never disturb the watchdog
            _log.warning("Alert webhook failed for %s/%s: %s",
                         alert.deployment_id, alert.kind, exc)
    return pushed
