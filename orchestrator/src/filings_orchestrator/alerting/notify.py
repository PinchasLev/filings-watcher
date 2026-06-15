"""Notification transport seam (ADR 0031).

The drainer holds a transport-neutral `Notification` value — severity, title,
body, structured fields — and hands it to a `Notifier`, the only component that
knows a wire format. `DiscordNotifier` renders it to Discord's webhook JSON and
POSTs it. A later move to Slack is one new `Notifier` implementation plus a
different webhook URL in Parameter Store; nothing upstream (the outbox, the
emit() call sites, the drainer's routing) changes.

The split is deliberate: a producer states a severity and never names a channel
or a provider; the drainer maps severity -> webhook URL and owns delivery
policy; the `Notifier` owns only rendering. Three concerns, three seams.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from filings_orchestrator.alerting.emit import ALERT

# Discord embed accent colors (decimal RGB). Red draws the eye for things that
# need action; a calm blue for situational awareness. Severity, not the
# provider, picks the intent; the provider picks how to express it.
_COLOR_ALERT = 0xE01E2B
_COLOR_INFO = 0x3B82F6


@dataclass(frozen=True)
class Notification:
    """A delivery-ready alert, independent of any provider's wire format."""

    severity: str
    title: str
    body: str | None
    fields: dict[str, object]


class Notifier(Protocol):
    """Renders a `Notification` to a provider and POSTs it to `webhook_url`.

    Implementations raise on a non-2xx response (or transport error) so the
    drainer can record the failure and retry the row on the next pass.
    """

    def send(self, webhook_url: str, notification: Notification) -> None: ...


class DiscordNotifier:
    """Renders notifications as Discord embeds and delivers via incoming webhook.

    Holds an injected `httpx.Client` so tests can supply a transport and the
    process can reuse one connection pool across a drain pass.
    """

    def __init__(self, client: httpx.Client, *, timeout: float = 10.0) -> None:
        self._client = client
        self._timeout = timeout

    def send(self, webhook_url: str, notification: Notification) -> None:
        response = self._client.post(
            webhook_url, json=_render_discord(notification), timeout=self._timeout
        )
        # 204 No Content is Discord's success for a webhook POST; any non-2xx
        # (rate limit, bad URL, outage) raises and the drainer retries the row.
        response.raise_for_status()


def _render_discord(notification: Notification) -> dict[str, object]:
    """Build the Discord webhook payload for one notification.

    Uses an embed (not a plain `content` string) so the accent color carries
    severity at a glance and structured fields render as a tidy grid. Empty
    `fields` simply omits the grid; a None `body` omits the description.
    """
    embed: dict[str, object] = {
        "title": notification.title,
        "color": _COLOR_ALERT if notification.severity == ALERT else _COLOR_INFO,
    }
    if notification.body:
        embed["description"] = notification.body
    if notification.fields:
        embed["fields"] = [
            {"name": key, "value": str(value), "inline": True}
            for key, value in notification.fields.items()
        ]
    return {"embeds": [embed]}
